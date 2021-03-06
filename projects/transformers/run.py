#  Numenta Platform for Intelligent Computing (NuPIC)
#  Copyright (C) 2021, Numenta, Inc.  Unless you have an agreement
#  with Numenta, Inc., for a separate license for this software code, the
#  following terms and conditions apply:
#
#  This program is free software you can redistribute it and/or modify
#  it under the terms of the GNU Affero Public License version 3 as
#  published by the Free Software Foundation.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
#  See the GNU Affero Public License for more details.
#
#  You should have received a copy of the GNU Affero Public License
#  along with this program.  If not, see htt"://www.gnu.org/licenses.
#
#  http://numenta.org/licenses/
#
"""
Pre-train or finetune HuggingFace models for masked language modeling
(BERT, ALBERT, RoBERTa...) on a text file or a dataset.

Here is the full list of checkpoints on the hub that can be fine-tuned by this script:
https://huggingface.co/models?filter=masked-lm

Adapted from huggingface/transformers/examples/run_mlm.py
"""

import logging
import math
import os
import sys
from hashlib import blake2b

import transformers
from datasets import concatenate_datasets, load_dataset, load_from_disk
from datasets.dataset_dict import DatasetDict
from transformers import (
    CONFIG_MAPPING,
    MODEL_FOR_MASKED_LM_MAPPING,
    AutoConfig,
    AutoModelForMaskedLM,
    AutoTokenizer,
    HfArgumentParser,
    Trainer,
    TrainerCallback,
    TrainingArguments,
    set_seed,
)
from transformers.trainer_utils import get_last_checkpoint, is_main_process

from experiments import CONFIGS
from nupic.research.frameworks.pytorch.model_utils import count_nonzero_params
from run_args import DataTrainingArguments, ModelArguments

logger = logging.getLogger(__name__)
MODEL_CONFIG_CLASSES = list(MODEL_FOR_MASKED_LM_MAPPING.keys())
MODEL_TYPES = tuple(conf.model_type for conf in MODEL_CONFIG_CLASSES)


def main():    # noqa: C901
    # See all possible arguments in src/transformers/training_args.py
    # or by passing the --help flag to this script.
    # We now keep distinct sets of args, for a cleaner separation of concerns.
    parser = HfArgumentParser(
        (ModelArguments, DataTrainingArguments, TrainingArguments)
    )

    # Add option to run from local experiment file
    parser.add_argument("-e", "--experiment", dest="experiment",
                        choices=list(CONFIGS.keys()),
                        help="Available experiments")
    args = parser.parse_args()

    trainer_callbacks = None
    if len(sys.argv) == 2 and sys.argv[1].endswith(".json"):
        # If we pass only one argument to the script and it's the path to a json file,
        # parse it to get our arguments.
        model_args, data_args, training_args = parser.parse_json_file(
            json_file=os.path.abspath(sys.argv[1])
        )
    elif "experiment" in args:
        # If experiment given, use arguments from experiment file instead
        config_dict = CONFIGS[args.experiment]
        config_dict["local_rank"] = args.local_rank  # added by torch.distributed.launch
        model_args, data_args, training_args = parser.parse_dict(config_dict)
        # Callbacks can only be passed when using experiment
        if "trainer_callbacks" in config_dict:
            trainer_callbacks = config_dict["trainer_callbacks"]
    else:
        model_args, data_args, training_args = parser.parse_args_into_dataclasses()

    # Detecting last checkpoint.
    last_checkpoint = None
    if (os.path.isdir(training_args.output_dir) and training_args.do_train
       and not training_args.overwrite_output_dir):
        last_checkpoint = get_last_checkpoint(training_args.output_dir)
        if last_checkpoint is None and len(os.listdir(training_args.output_dir)) > 0:
            raise ValueError(
                f"Output directory ({training_args.output_dir}) already exists and "
                "is not empty. Use --overwrite_output_dir to overcome."
            )
        elif last_checkpoint is not None:
            logger.info(
                f"Checkpoint detected, resuming training at {last_checkpoint}. To "
                "avoid this behavior, change the `--output_dir` or add "
                "`--overwrite_output_dir` to train from scratch."
            )

    # Setup logging
    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(name)s -   %(message)s",
        datefmt="%m/%d/%Y %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    logger.setLevel(logging.INFO if is_main_process(training_args.local_rank)
                    else logging.WARN)

    # Log on each process the small summary:
    logger.warning(
        f"Process rank: {training_args.local_rank}, "
        f"device: {training_args.device}, n_gpu: {training_args.n_gpu} "
        f"distributed training: {bool(training_args.local_rank != -1)}, "
        f"16-bits training: {training_args.fp16}"
    )
    # Set the verbosity to info of the Transformers logger (on main process only):
    if is_main_process(training_args.local_rank):
        transformers.utils.logging.set_verbosity_info()
        transformers.utils.logging.enable_default_handler()
        transformers.utils.logging.enable_explicit_format()
    logger.info("Training/evaluation parameters %s", training_args)
    logger.info("Model parameters: %s", model_args)
    logger.info("Data parameters: %s", data_args)

    # Set seed before initializing model.
    set_seed(training_args.seed)
    logger.info(f"Seed to reproduce: {training_args.seed}")

    # Get the datasets: you can either provide your own CSV/JSON/TXT training
    # and evaluation files (see below) or just provide the name of one of the public
    # datasets available on the hub at https://huggingface.co/datasets/
    # (the dataset will be downloaded automatically from the datasets Hub
    #
    # For CSV/JSON files, this script will use the column called 'text' or the first
    # column. You can easily tweak this behavior (see below)
    #
    # In distributed training, the load_dataset function guarantee that only one local
    # process can concurrently download the dataset.

    tokenized_datasets, dataset_path = None, None
    if data_args.dataset_name is not None:

        # Account for when a single dataset is passed as argument
        if not(isinstance(data_args.dataset_name, tuple)):
            data_args.dataset_name = (data_args.dataset_name, )
        if not (isinstance(data_args.dataset_config_name, tuple)):
            data_args.dataset_config_name = (data_args.dataset_config_name, )

        assert len(data_args.dataset_name) == len(data_args.dataset_config_name), \
            ("If using more than one dataset, length of dataset_name and "
             "dataset_config_name must match")

        # Verifies if dataset is already saved
        dataset_folder = "-".join([
            f"{name}_{config}" for name, config in
            zip(data_args.dataset_name, data_args.dataset_config_name)
        ])
        dataset_folder = blake2b(dataset_folder.encode(), digest_size=20).hexdigest()
        dataset_path = os.path.join(
            os.path.abspath(data_args.tokenized_data_cache_dir),
            str(dataset_folder)
        )
        logger.info(f"Tokenized dataset cache folder: {dataset_path}")

        if os.path.exists(dataset_path) and data_args.reuse_tokenized_data:
            logger.info(f"Loading cached tokenized data ...")
            tokenized_datasets = load_from_disk(dataset_path)
        else:
            datasets = load_datasets(data_args)

    else:
        # If dataset not available in Hub, look for train and validation files.
        data_files = {}
        if data_args.train_file is not None:
            data_files["train"] = data_args.train_file
        if data_args.validation_file is not None:
            data_files["validation"] = data_args.validation_file
        extension = data_args.train_file.split(".")[-1]
        if extension == "txt":
            extension = "text"
        datasets = load_dataset(extension, data_files=data_files)

    # See more about loading any type of standard or custom dataset
    # (from files, python dict, pandas DataFrame, etc) at
    # https://huggingface.co/docs/datasets/loading_datasets.html.

    # Load pretrained model and tokenizer
    #
    # Distributed training:
    # The .from_pretrained methods guarantee that only one local process can
    # concurrently download model & vocab.
    config_kwargs = {
        "cache_dir": model_args.cache_dir,
        "revision": model_args.model_revision,
        "use_auth_token": True if model_args.use_auth_token else None,
    }
    if model_args.config_name:
        config = AutoConfig.from_pretrained(model_args.config_name, **config_kwargs)
    elif model_args.model_name_or_path:
        config = AutoConfig.from_pretrained(
            model_args.model_name_or_path, **config_kwargs
        )
    else:
        config = CONFIG_MAPPING[model_args.model_type]()
        logger.warning("You are instantiating a new config instance from scratch.")

    tokenizer_kwargs = {
        "cache_dir": model_args.cache_dir,
        "use_fast": model_args.use_fast_tokenizer,
        "revision": model_args.model_revision,
        "use_auth_token": True if model_args.use_auth_token else None,
    }
    if model_args.tokenizer_name:
        tokenizer = AutoTokenizer.from_pretrained(
            model_args.tokenizer_name, **tokenizer_kwargs
        )
    elif model_args.model_name_or_path:
        tokenizer = AutoTokenizer.from_pretrained(
            model_args.model_name_or_path, **tokenizer_kwargs
        )
    else:
        raise ValueError(
            "You are instantiating a new tokenizer from scratch. This is not supported "
            "by this script. You can do it from another script, save it, and load it "
            "from here, using --tokenizer_name."
        )

    if model_args.model_name_or_path:
        model = AutoModelForMaskedLM.from_pretrained(
            model_args.model_name_or_path,
            from_tf=bool(".ckpt" in model_args.model_name_or_path),
            config=config,
            cache_dir=model_args.cache_dir,
            revision=model_args.model_revision,
            use_auth_token=True if model_args.use_auth_token else None,
        )
    else:
        logger.info("Training new model from scratch")
        model = AutoModelForMaskedLM.from_config(config)

    model.resize_token_embeddings(len(tokenizer))

    # Preprocessing the datasets. First tokenize all the texts, if not already tokenized
    if tokenized_datasets is None:
        if training_args.do_train:
            column_names = datasets["train"].column_names
        else:
            column_names = datasets["validation"].column_names
        text_column_name = "text" if "text" in column_names else column_names[0]

        logger.info(f"Tokenizing datasets ...")
        tokenized_datasets = preprocess_datasets(
            datasets, tokenizer, column_names, text_column_name, data_args
        )

        # Save only if a dataset_path has been defined in the previous steps
        # that will be True only when loading from dataset hub
        if data_args.save_tokenized_data and dataset_path is not None:
            logger.info(f"Saving tokenized dataset to {dataset_path}")
            tokenized_datasets.save_to_disk(dataset_path)

    # Verifying fingerprint for caching
    logger.info(f"Dataset fingerprint: {tokenized_datasets['train']._fingerprint}")

    # Data collator will take care of randomly masking the tokens.
    assert hasattr(transformers, data_args.data_collator), \
        f"Data collator {data_args.data_collator} not available"

    data_collator = getattr(transformers, data_args.data_collator)(
        tokenizer=tokenizer, mlm_probability=data_args.mlm_probability
    )

    if trainer_callbacks is not None:
        for cb in trainer_callbacks:
            assert isinstance(cb, TrainerCallback), \
                "Trainer callbacks must be an instance of TrainerCallback"

    # Initialize Trainer
    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=tokenized_datasets["train"] if training_args.do_train else None,
        eval_dataset=(tokenized_datasets["validation"] if training_args.do_eval
                      else None),
        tokenizer=tokenizer,
        data_collator=data_collator,
        callbacks=trainer_callbacks,
    )

    logger.info("Total params: {:,} Non zero params: {:,}".format(
        *count_nonzero_params(trainer.model)
    ))

    # Training
    if training_args.do_train:
        if last_checkpoint is not None:
            checkpoint = last_checkpoint
        elif (model_args.model_name_or_path is not None
              and os.path.isdir(model_args.model_name_or_path)):
            checkpoint = model_args.model_name_or_path
        else:
            checkpoint = None
        train_result = trainer.train(resume_from_checkpoint=checkpoint)
        trainer.save_model()  # Saves the tokenizer too for easy upload

        output_train_file = os.path.join(training_args.output_dir, "train_results.txt")
        if trainer.is_world_process_zero():
            with open(output_train_file, "w") as writer:
                logger.info("***** Train results *****")
                for key, value in sorted(train_result.metrics.items()):
                    logger.info(f"  {key} = {value}")
                    writer.write(f"{key} = {value}\n")

            # Need to save the state, since Trainer.save_model saves only the tokenizer
            # with the model
            trainer.state.save_to_json(
                os.path.join(training_args.output_dir, "trainer_state.json")
            )

    # Evaluation
    results = {}
    if training_args.do_eval:
        logger.info("*** Evaluate ***")

        eval_output = trainer.evaluate()

        perplexity = math.exp(eval_output["eval_loss"])
        results["perplexity"] = perplexity

        output_eval_file = os.path.join(
            training_args.output_dir, "eval_results_mlm.txt"
        )
        if trainer.is_world_process_zero():
            with open(output_eval_file, "w") as writer:
                logger.info("***** Eval results *****")
                for key, value in sorted(results.items()):
                    logger.info(f"  {key} = {value}")
                    writer.write(f"{key} = {value}\n")

    return results


def _mp_fn(index):
    # For xla_spawn (TPUs)
    main()


def load_datasets(data_args):
    """Load and concatenate multiple compatible datasets"""
    train_datasets, validation_datasets = [], []
    for name, config in zip(data_args.dataset_name, data_args.dataset_config_name):

        dataset = load_dataset(name, config)
        if "validation" not in dataset.keys():
            validation_ds = load_dataset(
                name, config,
                split=f"train[:{data_args.validation_split_percentage}%]",
            )
            train_ds = load_dataset(
                name, config,
                split=f"train[{data_args.validation_split_percentage}%:]",
            )
        else:
            validation_ds = dataset["validation"]
            train_ds = dataset["train"]

        # Some specific preprocessing to align fields on known datasets
        # extraneous fields not used in language modelling are also removed
        # after preprocessing
        if name == "wikipedia":
            train_ds.remove_columns_("title")
            validation_ds.remove_columns_("title")
        elif name == "ptb_text_only":
            train_ds.rename_column_("sentence", "text")
            validation_ds.rename_column_("sentence", "text")

        train_datasets.append(train_ds)
        validation_datasets.append(validation_ds)

    for ds_idx in range(1, len(train_datasets)):
        assert train_datasets[ds_idx].features.type == \
            train_datasets[ds_idx - 1].features.type, \
            "Features name and type must match between all datasets"

    datasets = DatasetDict()
    datasets["train"] = concatenate_datasets(train_datasets)
    datasets["validation"] = concatenate_datasets(validation_datasets)

    return datasets


def preprocess_datasets(datasets, tokenizer, column_names, text_column_name, data_args):
    """Tokenize datasets and applies remaining preprocessing steps"""

    if data_args.line_by_line:
        # When using line_by_line, we just tokenize each nonempty line.
        padding = "max_length" if data_args.pad_to_max_length else False

        def tokenize_function(examples):
            # Remove empty lines
            examples["text"] = [
                line for line in examples["text"]
                if len(line) > 0 and not line.isspace()
            ]
            return tokenizer(
                examples["text"],
                padding=padding,
                truncation=True,
                max_length=data_args.max_seq_length,
                # We use this option because DataCollatorForLanguageModeling (see below)
                # is more efficient when it receives the `special_tokens_mask`.
                return_special_tokens_mask=True,
            )

        tokenized_datasets = datasets.map(
            tokenize_function,
            batched=True,
            num_proc=data_args.preprocessing_num_workers,
            remove_columns=[text_column_name],
            load_from_cache_file=not data_args.overwrite_cache,
        )
    else:
        # Otherwise, we tokenize every text, then concatenate them together before
        # splitting them in smaller parts. We use `return_special_tokens_mask=True`
        # because DataCollatorForLanguageModeling (see below) is more
        # efficient when it receives the `special_tokens_mask`.
        def tokenize_function(examples):
            return tokenizer(
                examples[text_column_name], return_special_tokens_mask=True
            )

        tokenized_datasets = datasets.map(
            tokenize_function,
            batched=True,
            num_proc=data_args.preprocessing_num_workers,
            remove_columns=column_names,
            load_from_cache_file=not data_args.overwrite_cache,
        )

        if data_args.max_seq_length is None:
            max_seq_length = tokenizer.model_max_length
            if max_seq_length > 1024:
                logger.warn(
                    f"The tokenizer picked seems to have a very large "
                    f"`model_max_length` ({tokenizer.model_max_length}). "
                    f"Picking 1024 instead. You can change that default value by "
                    f"passing --max_seq_length xxx. "
                )
                max_seq_length = 1024
        else:
            if data_args.max_seq_length > tokenizer.model_max_length:
                logger.warn(
                    f"The max_seq_length passed ({data_args.max_seq_length}) is larger "
                    f"than the maximum length for the model "
                    f"({tokenizer.model_max_length}). "
                    f"Using max_seq_length={tokenizer.model_max_length}. "
                )
            max_seq_length = min(data_args.max_seq_length, tokenizer.model_max_length)

        # Main data processing function that will concatenate all texts from our dataset
        # and generate chunks of max_seq_length.
        def group_texts(examples):
            # Concatenate all texts.
            concatenated_examples = {k: sum(examples[k], []) for k in examples.keys()}
            total_length = len(concatenated_examples[list(examples.keys())[0]])
            # We drop the small remainder, we could add padding if the model supported
            # it instead of this drop, you can customize this part to your needs.
            total_length = (total_length // max_seq_length) * max_seq_length
            # Split by chunks of max_len.
            result = {
                k: [t[i : i + max_seq_length] for i in
                    range(0, total_length, max_seq_length)]
                for k, t in concatenated_examples.items()
            }
            return result

        # Note that with `batched=True`, this map processes 1,000 texts together, so
        # group_texts throws away a remainder for each of those groups of 1,000 texts.
        # You can adjust that batch_size here but a higher value might be slower
        # to preprocess.
        #
        # To speed up this part, we use multiprocessing. See the documentation of the
        # map method for more information:
        tokenized_datasets = tokenized_datasets.map(
            group_texts,
            batched=True,
            num_proc=data_args.preprocessing_num_workers,
            load_from_cache_file=not data_args.overwrite_cache,
        )

    return tokenized_datasets


if __name__ == "__main__":
    main()
