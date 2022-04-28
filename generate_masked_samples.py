from typing import List, Tuple

from dataloader import WikipediaDataModule
from model import DocumentProfileMatchingTransformer

import argparse
import os
import sys
import torch

from collections import OrderedDict

import datasets
import numpy as np
import pandas as pd
import textattack
import transformers

from textattack import Attack
from textattack import Attacker
from textattack import AttackArgs
from textattack.attack_results import SuccessfulAttackResult
from textattack.constraints.pre_transformation import RepeatModification
from textattack.loggers import CSVLogger
from textattack.shared import AttackedText
from tqdm import tqdm


class ChangeClassificationToBelowTopKClasses(textattack.goal_functions.ClassificationGoalFunction):
    k: int
    def __init__(self, *args, k: int = 1, **kwargs):
        self.k = k
        super().__init__(*args, **kwargs)

    def _is_goal_complete(self, model_output, _):
        original_class_score = model_output[self.ground_truth_output]
        num_better_classes = (model_output > original_class_score).sum()
        return num_better_classes >= self.k

    def _get_score(self, model_output, _):
        return 1 - model_output[self.ground_truth_output]
    
    
    """have to reimplement the following method to change the precision on the sum-to-one condition."""
    def _process_model_outputs(self, inputs, scores):
        """Processes and validates a list of model outputs.
        This is a task-dependent operation. For example, classification
        outputs need to have a softmax applied.
        """
        # Automatically cast a list or ndarray of predictions to a tensor.
        if isinstance(scores, list):
            scores = torch.tensor(scores)

        # Ensure the returned value is now a tensor.
        if not isinstance(scores, torch.Tensor):
            raise TypeError(
                "Must have list, np.ndarray, or torch.Tensor of "
                f"scores. Got type {type(scores)}"
            )

        # Validation check on model score dimensions
        if scores.ndim == 1:
            # Unsqueeze prediction, if it's been squeezed by the model.
            if len(inputs) == 1:
                scores = scores.unsqueeze(dim=0)
            else:
                raise ValueError(
                    f"Model return score of shape {scores.shape} for {len(inputs)} inputs."
                )
        elif scores.ndim != 2:
            # If model somehow returns too may dimensions, throw an error.
            raise ValueError(
                f"Model return score of shape {scores.shape} for {len(inputs)} inputs."
            )
        elif scores.shape[0] != len(inputs):
            # If model returns an incorrect number of scores, throw an error.
            raise ValueError(
                f"Model return score of shape {scores.shape} for {len(inputs)} inputs."
            )
        elif not ((scores.sum(dim=1) - 1).abs() < 1e-4).all():
            # Values in each row should sum up to 1. The model should return a
            # set of numbers corresponding to probabilities, which should add
            # up to 1. Since they are `torch.float` values, allow a small
            # error in the summation.
            scores = torch.nn.functional.softmax(scores, dim=1)
            if not ((scores.sum(dim=1) - 1).abs() < 1e-4).all():
                raise ValueError("Model scores do not add up to 1.")
        return scores.cpu()

class WordSwapSingleWord(textattack.transformations.word_swap.WordSwap):
    """Takes a sentence and transforms it by replacing with a single fixed word.
    """
    single_word: str
    def __init__(self, single_word: str = "?", **kwargs):
        super().__init__(**kwargs)
        self.single_word = single_word

    def _get_replacement_words(self, _word: str):
        return [self.single_word]

class CustomCSVLogger(CSVLogger):
    """Logs attack results to a CSV."""

    def log_attack_result(self, result: textattack.goal_function_results.ClassificationGoalFunctionResult):
        original_text, perturbed_text = result.diff_color(self.color_method)
        original_text = original_text.replace("\n", AttackedText.SPLIT_TOKEN)
        perturbed_text = perturbed_text.replace("\n", AttackedText.SPLIT_TOKEN)
        result_type = result.__class__.__name__.replace("AttackResult", "")
        row = {
            "original_person": result.original_result._processed_output[0],
            "original_text": original_text,
            "perturbed_person": result.perturbed_result._processed_output[0],
            "perturbed_text": perturbed_text,
            "original_score": result.original_result.score,
            "perturbed_score": result.perturbed_result.score,
            "original_output": result.original_result.output,
            "perturbed_output": result.perturbed_result.output,
            "ground_truth_output": result.original_result.ground_truth_output,
            "num_queries": result.num_queries,
            "result_type": result_type,
        }
        self.df = pd.concat([self.df, pd.DataFrame([row])], ignore_index=True)
        self._flushed = False

class WikiDataset(textattack.datasets.Dataset):
    dataset: datasets.Dataset
    
    def __init__(self, dm: WikipediaDataModule):
        self.shuffled = True
        # filter out super long examples
        self.dataset = [ex for ex in dm.val_dataset if ex['document'].count(' ') < 100]
        self.label_names = list(dm.val_dataset['name'])
    
    def __len__(self) -> int:
        return len(self.dataset)
    
    def __getitem__(self, i: int) -> Tuple[OrderedDict, int]:
        input_dict = OrderedDict([
            ('document', self.dataset[i]['document'])
        ])
        return input_dict, self.dataset[i]['text_key_id']

class MyModelWrapper(textattack.models.wrappers.ModelWrapper):
    model: DocumentProfileMatchingTransformer
    profile_embeddings: torch.Tensor
    max_seq_length: int
    
    def __init__(self, model: DocumentProfileMatchingTransformer, max_seq_length: int = 64):
        self.model = model
        self.model.eval()
        self.profile_embeddings = model.val_profile_embeddings.clone().detach()
        self.max_seq_length = max_seq_length
                 
    def to(self, device):
        self.model.to(device)
        self.profile_embeddings.to(device)
        return self # so semantics `model = MyModelWrapper().to('cuda')` works properly

    def __call__(self, text_input_list, batch_size=32):
        model_device = next(self.model.parameters()).device
        
        # TODO: implement batch size if we start running out of memory here.
        with torch.no_grad():
            document_embeddings = self.model.forward_document_text(text=text_input_list)
            document_to_profile_logits = document_embeddings @ self.profile_embeddings.T.to(model_device) * (1/10.)
            document_to_profile_probs = torch.nn.functional.softmax(
                document_to_profile_logits, dim=-1
            )
        assert document_to_profile_probs.shape == (len(text_input_list), len(self.profile_embeddings))
        return document_to_profile_probs


def precompute_profile_embeddings(
    model: DocumentProfileMatchingTransformer,
    dm: WikipediaDataModule
    ):
    model.profile_model.cuda()
    model.profile_model.eval()
    print('Precomputing profile embeddings before first epoch...')
    # no need to compute train embeddings in this setting
    # - we only use val embeddings for inference
    #
    model.train_profile_embeddings = np.zeros((len(dm.train_dataset), model.profile_embedding_dim))
    # for train_batch in tqdm(dm.train_dataloader(), desc="[1/2] Precomputing train embeddings", colour="magenta", leave=False):
    #     with torch.no_grad():
    #         profile_embeddings = model.forward_profile_text(text=train_batch["profile"])
    #     model.train_profile_embeddings[train_batch["text_key_id"]] = profile_embeddings.cpu()
    model.train_profile_embeddings = torch.tensor(model.train_profile_embeddings, dtype=torch.float32)

    model.val_profile_embeddings = np.zeros((len(dm.val_dataset), model.profile_embedding_dim))
    for val_batch in tqdm(dm.val_dataloader(), desc="[2/2] Precomputing val embeddings", colour="green", leave=False):
        with torch.no_grad():
            profile_embeddings = model.forward_profile_text(text=val_batch["profile"])
        model.val_profile_embeddings[val_batch["text_key_id"]] = profile_embeddings.cpu()
    model.val_profile_embeddings = torch.tensor(model.val_profile_embeddings, dtype=torch.float32)
    model.profile_model.train()


def main(k: int, n: int):
    checkpoint_path = '/home/jxm3/research/deidentification/unsupervised-deidentification/saves/roberta__distilbert-base-uncased__dropout_0.8_0.8/deid-wikibio_default/3nbt75gp_171/checkpoints/epoch=20-step=382387.ckpt'
    model = DocumentProfileMatchingTransformer.load_from_checkpoint(
        checkpoint_path,
        document_model_name_or_path="roberta-base",
        profile_model_name_or_path="distilbert-base-uncased",
        train_batch_size=64,
        eval_batch_size=64,
        max_seq_length=256,
        pretrained_profile_encoder=False,
        redaction_strategy="",
        dataset_name='wiki_bio',
        num_workers=1,
        word_dropout_ratio=0.0, word_dropout_perc=0.0,
    )

    num_cpus = os.cpu_count()

    dm = WikipediaDataModule(
        mask_token=model.document_tokenizer.mask_token,
        dataset_name='wiki_bio',
        dataset_train_split='train[:10%]', # this model was trained with 40% of training data
        dataset_val_split='val[:20%]',
        dataset_version='1.2.0',
        num_workers=1,
        train_batch_size=64,
        eval_batch_size=64,
        max_seq_length=256,
        redaction_strategy=""
    )
    dm.setup("fit")

    dataset = WikiDataset(dm)

    precompute_profile_embeddings(model, dm)
    model_wrapper = MyModelWrapper(model)
    model_wrapper.to('cuda')

    constraints = [RepeatModification()]
    transformation = WordSwapSingleWord(single_word=model.document_tokenizer.mask_token)
    # search_method = textattack.search_methods.GreedyWordSwapWIR()
    search_method = textattack.search_methods.BeamSearch(beam_width=4)

    print(f'***Attacking with k={k} n={n}***')
    goal_function = ChangeClassificationToBelowTopKClasses(model_wrapper, k=k)
    attack = Attack(
        goal_function, constraints, transformation, search_method
    )
    attack_args = AttackArgs(num_examples=n, disable_stdout=True)
    attacker = Attacker(attack, dataset, attack_args)

    results_iterable = attacker.attack_dataset()

    logger = CustomCSVLogger(color_method=None)

    for result in results_iterable:
        logger.log_attack_result(result)
    
    logger.df.to_csv(f'adv_csvs/results_{k}_{n}.csv')
    

def get_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description='Generate adversarially-masked examples for a model.',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument('--k', type=int, default=8,
        help='top-K classes for adversarial goal function')
    parser.add_argument('--n', type=int, default=1000,
        help='number of examples to run on')

    args = parser.parse_args()
    return args


if __name__ == '__main__':
    args = get_args()
    main(k=args.k, n=args.n)