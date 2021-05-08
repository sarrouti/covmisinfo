#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Wed Aug 19 01:31:42 2020

@author: sarroutim2
"""

import functools
import itertools
import os
import re
import time

from absl import logging
import mesh_tensorflow.transformer.dataset as transformer_dataset
import t5.data
from t5.models.t5_model import T5Model
import tensorflow.compat.v1 as tf
import tensorflow_datasets as tfds
import torch
import torch.utils.tensorboard
import transformers
from tools import get_dataset
from tools import write_lines_to_file
from tools import tokens_to_batches
from sklearn.metrics import accuracy_score, f1_score, precision_score, recall_score,classification_report

CHECKPOINT_FILE_FORMAT = "model-{}.checkpoint"

class T5Classifier(T5Model):
  """Wrapper class for Hugging Face Transformers PyTorch T5 model."""

  def __init__(self, model_spec, model_dir, device):
    """Constructor for HfModel class.

    Args:
      model_spec: A str to pass into the `pretrained_model_name_or_path`
        argument of `transformers.T5ForConditionalGeneration.from_pretrained`
        (e.g. `"t5-base"` or a path to a previously trained model) or an
        instance of the `transformers.configuration_t5.T5Config` class to use
        to directly construct the `transformers.T5ForConditionalGeneration`
        object.
      model_dir: str, directory to save and load model checkpoints.
      device: `torch.device` on which the model should be run.
    """
    # We have to import transformers here because it has a side effect of
    # creating a TensorFlow graph, which prevents eager execution from being
    # enabled in files that import hf_model.py
      # pylint: disable=import-outside-toplevel,g-import-not-at-top
    if isinstance(model_spec, str):
      self._model = transformers.T5ForConditionalGeneration.from_pretrained(
          model_spec
      )
    elif isinstance(model_spec, transformers.T5Config):
      self._model = transformers.T5ForConditionalGeneration(model_spec)
    else:
      raise ValueError("model_spec should be a string or T5Config.")
    
    tf.io.gfile.makedirs(model_dir)
    self._writer = torch.utils.tensorboard.writer.SummaryWriter(model_dir)
    self._model_dir = model_dir
    self._device = device
    if self._device.type == "cuda":
      self._model.cuda()
    self._step = 0
    self.load_latest_checkpoint()
    self.to_tensor = functools.partial(torch.as_tensor, device=self._device)
  @property
  def model(self):
    return self._model

  @property
  def step(self):
    return self._step

  def save_checkpoint(self, step):
    """Save the current model parameters to the `model_dir`.

    Args:
      step: int, the current training step.
    """
    path = os.path.join(self._model_dir, CHECKPOINT_FILE_FORMAT.format(step))
    torch.save(self._model.state_dict(), path)

  def load_checkpoint(self, step, model_dir=None):
    """Load the model parameters from a checkpoint at a given step.

    Args:
      step: int, load the checkpoint from this training step.
      model_dir: str, the directory of the checkpoint to load or None to use
        this model's directory.
    """
    model_dir = model_dir or self._model_dir
    path = os.path.join(model_dir, CHECKPOINT_FILE_FORMAT.format(step))
    logging.info("Loading from %s", path)
    self._model.load_state_dict(torch.load(path))
    self._step = step

  def get_all_checkpoint_steps(self, model_dir=None):
    """Retrieve the steps corresponding to all checkpoints in `model_dir`.

    Args:
      model_dir: str, the directory of the checkpoints or None to use this
        model's directory.

    Returns:
      A list of ints corresponding to all checkpoint steps, or None if there
        are no checkpoints in the model directory.
    """
    model_dir = model_dir or self._model_dir
    checkpoint_files = tf.io.gfile.glob(
        os.path.join(model_dir, CHECKPOINT_FILE_FORMAT.format("*"))
    )
    if not checkpoint_files:
      return
    step_regex = re.compile(".*" + CHECKPOINT_FILE_FORMAT.format(r"(\d+)"))
    steps = [int(step_regex.match(path).group(1)) for path in checkpoint_files]
    return sorted(steps)

  def get_latest_checkpoint_step(self, model_dir=None):
    """Retrieve the step corresponding to the most recent checkpoint.

    Args:
      model_dir: str, the directory of the checkpoints or None to use this
        model's directory.

    Returns:
      An integer corresponding to the most recent step, or None if there are no
      checkpoints in the model directory.
    """
    steps = self.get_all_checkpoint_steps(model_dir)
    if steps is not None:
      return max(steps)

  def load_latest_checkpoint(self):
    """Load the most recent checkpoint and update the model's current step."""
    latest_step = self.get_latest_checkpoint_step()
    if latest_step is not None:
      self.load_checkpoint(latest_step)
  def train(
      self,
      mixture_or_task_name,
      steps,
      save_steps,
      sequence_length,
      split,
      batch_size,
      optimizer,
      learning_rate_scheduler=None,
    ):
    self._model.train()
    ds = get_dataset(mixture_or_task_name, sequence_length, split, batch_size)
    # Repeat dataset forever
    ds = itertools.cycle(ds)
    optimizer = optimizer(self._model.parameters())
    if learning_rate_scheduler:
      learning_rate_scheduler = learning_rate_scheduler(optimizer)

    now = time.time()
    for train_step, batch in enumerate(itertools.islice(ds, steps)):

      if not train_step % save_steps:
        # TODO(craffel): Consider saving optimizer and scheduler state.
        logging.info("Saving checkpoint for step %s", self._step)
        self.save_checkpoint(self._step)
      self._model.zero_grad()
      outputs = self._model(
          input_ids=self.to_tensor(batch["inputs"]),
          attention_mask=self.to_tensor(batch["inputs_mask"]),
          decoder_attention_mask=self.to_tensor(batch["targets_mask"]),
          lm_labels=self.to_tensor(batch["targets"]),
      )
      loss = outputs[0]
      loss.backward()
      optimizer.step()
      if learning_rate_scheduler:
        learning_rate_scheduler.step()

      self._writer.add_scalar(
          "loss", loss.detach().cpu().numpy(), self._step
      )
      self._writer.add_scalar("step/s", 1 / (time.time() - now), self._step)
      if not train_step % save_steps:
        # TODO(craffel): Consider saving optimizer and scheduler state.
        logging.info("Loss: %s", loss.detach().cpu().numpy())

      now = time.time()
      self._step += 1
    logging.info("Saving final checkpoint for step %s", self._step)
    self.save_checkpoint(self._step)
    
  def eval(
      self,
      mixture_or_task_name,
      sequence_length,
      batch_size,
      checkpoint_steps=None,
      summary_dir=None,
      split="validation",
      **generate_kwargs,
  ):
    """Evaluate the model on the given Mixture or Task.

    *Note*: If a checkpoint step is provided (i.e. `checkpoint_steps is not
    None`), the model's state will be replaced by the state in those
    checkpoints. If you have not saved your model before calling `eval`, you
    should call `save_checkpoint` before `eval` to avoid losing its parameter
    values and state.

    Args:
      mixture_or_task_name: str, the name of the Mixture or Task to evaluate
        on.  Must be pre-registered in the global `t5.data.TaskRegistry` or
        `t5.data.MixtureRegistry.`
      sequence_length: dict of int, a dict mapping feature name to length.
      batch_size: int, the number of padded sequences in each batch.
      checkpoint_steps: int, list of ints, "all", or None. If None, eval in the
        model in its current state without loading any checkpoints. If an int
        or list of ints, evaluation will be run on the checkpoint files in
        `model_dir` whose global steps are those provided. If -1, eval on the
        latest checkpoint from the model directory. If "all", evaluate all
        checkpoints in the model directory.
      summary_dir: str, path to write TensorBoard events file summaries for
        eval. If None, use model_dir/{split}_eval.
      split: str, the mixture/task split to evaluate on.
      **generate_kwargs: Additional keyword arguments to pass to
        `transformers.PretrainedModel.generate()`, for example to change the
        decoding strategy. See the documentation for
        `transformers.PretrainedModel.generate()` for options.
    """
    mixture_or_task = t5.data.get_mixture_or_task(mixture_or_task_name)
    vocab = mixture_or_task.output_features["targets"].vocabulary

    if isinstance(mixture_or_task, t5.data.Mixture):
      tasks = mixture_or_task.tasks
    elif isinstance(mixture_or_task, t5.data.Task):
      tasks = [mixture_or_task]

    for task in tasks:
      if split not in task.splits:
        logging.info(
            "Task %s has no '%s' split; skipping eval.", task.name, split
        )
    tasks = [task for task in tasks if split in task.splits]

    summary_dir = summary_dir or os.path.join(self._model_dir, f"{split}_eval")
    tf.io.gfile.makedirs(summary_dir)

    def _unbatch(batch):
      """Converts a dict of lists to a list of dicts of singletons."""
      return [dict(zip(batch, t)) for t in zip(*batch.values())]

    # Pre-load in all of the targets once before doing eval
    cached_targets = {}
    cached_examples = {}
    for task in tasks:
      if task.metric_fns:
        ds = get_dataset(task.name, sequence_length, split, batch_size)
        # Create list of postprocessed text targets
        batches = list(ds)
        if not batches:
          raise ValueError(f"The '{split}' split of {task.name} is empty.")
        # "Unbatch" the dataset
        examples = [ex for b in batches for ex in _unbatch(b)]  # pylint:disable=g-complex-comprehension
        targets = [
            task.postprocess_fn(  # pylint:disable=g-complex-comprehension
                tf.compat.as_text(ex["targets_plaintext"]),
                example=ex,
                is_target=True
            ) for ex in examples
        ]
        targets_filename = os.path.join(summary_dir, f"{task.name}_targets")
        write_lines_to_file(targets, targets_filename)

        inputs_filename = os.path.join(summary_dir, f"{task.name}_inputs")
        inputs = [ex["inputs_plaintext"] for ex in examples]
        write_lines_to_file(inputs, inputs_filename)

        cached_targets[task.name] = targets
        cached_examples[task.name] = batches

    def _eval_current_model():
      self._model.eval()
      for task in tasks:
        ds = cached_examples[task.name]
        targets = cached_targets[task.name]
        predictions = []
        for batch in ds:
          predicted_tokens = self._model.generate(
              input_ids=self.to_tensor(batch["inputs"]), **generate_kwargs
          )
          predicted_tokens = predicted_tokens.cpu().numpy().tolist()
          predictions.extend(
              [
                  task.postprocess_fn(vocab.decode(p), example=ex)
                  for p, ex in zip(predicted_tokens, _unbatch(batch))
              ]
          )

        if len(targets) != len(predictions):
          raise ValueError(
              f"#targets ({len(targets)}) != #predictions ({len(predictions)})"
          )

        predictions_file = os.path.join(
            summary_dir, f"{task.name}_{self._step}_predictions"
        )
        write_lines_to_file(predictions, predictions_file)

        for metric_fn in task.metric_fns:
          scores = metric_fn(targets, predictions)
          for metric_name, metric_value in scores.items():
            tag = f"eval/{task.name}/{metric_name}"
            self._writer.add_scalar(tag, metric_value, self._step)
            logging.info(
                "%s at step %d: %.3f Acc: %.4f", tag, self._step, metric_value, accuracy_score(targets,predictions)
            )
            print(
                "F1_score: %.6f: ", f1_score(targets,predictions,average='macro')
            )
            print(
                "Precision: %.6f: ", precision_score(targets,predictions,average='macro')
            )
            print(
                "Recall: %.6f: ", recall_score(targets,predictions,average='macro')
            )
            #print(
             #   "classification_report:", classification_report(targets,predictions)
            #)
            

        self._writer.flush()

    if checkpoint_steps is None:
      _eval_current_model()
      return
    elif isinstance(checkpoint_steps, int):
      checkpoint_steps = [checkpoint_steps]
    elif checkpoint_steps == "all":
      checkpoint_steps = self.get_all_checkpoint_steps()
    elif not isinstance(checkpoint_steps, (list, tuple)):
      raise ValueError(
          f"checkpoint_steps must be None, int or list; got {checkpoint_steps}"
      )
    for checkpoint_step in checkpoint_steps:
      self.load_checkpoint(checkpoint_step)
      _eval_current_model()

  def predict(
      self,
      inputs,
      sequence_length,
      batch_size,
      output_file=None,
      vocabulary=None,
      **generate_kwargs,
  ):
    """Evaluate the model on the given Mixture or Task.

    *Note*: If a checkpoint step is provided (i.e. `checkpoint_steps is not
    None`), the model's state will be replaced by the state in those
    checkpoints. If you have not saved your model before calling `eval`, you
    should call `save_checkpoint` before `eval` to avoid losing its parameter
    values and state.

    Args:
      inputs: list of str or str, either a list of inputs to feed into the
        model or the path to a text file that contains a single input on each
        line.
      sequence_length: dict of int, a dict mapping feature name to length.
      batch_size: int, the number of padded sequences in each batch.
      output_file: str or None, path to write out predictions or None to skip
        writing.
      vocabulary: t5.data.vocabularies.Vocabulary or dict or None. Either the
        Vocabulary to use for processing inputs and targets, a dict mapping
        "inputs" to a Vocabulary for encoding the inputs and "targets" for
        decoding the predictions, or None (default) to use a
        t5.data.sentencepiece_vocabulary.SentencePieceVocabulary with the
        provided sentencepiece_model_path (as was used in all pre-trained T5
        models).
      **generate_kwargs: Additional keyword arguments to pass to
        `transformers.PretrainedModel.generate()`, for example to change the
        decoding strategy. See the documentation for
        `transformers.PretrainedModel.generate()` for options.
    """
    if isinstance(inputs, str):
      if not tf.io.gfile.exists(inputs):
        raise ValueError(
            f"A str was provided for `inputs`, but the path {inputs} does not "
            "exist. If you want the model's output for {inputs}, you should "
            "feed in inputs=['{inputs}']"
        )
      with tf.io.gfile.GFile(inputs) as f:
        inputs = [l.strip() for l in f]

    if vocabulary is None:
      vocab = t5.data.get_default_vocabulary()
      vocabs = {"inputs": vocab, "targets": vocab}
    elif isinstance(vocabulary, t5.data.vocabularies.Vocabulary):
      vocabs = {"inputs": vocabulary, "targets": vocabulary}
    elif isinstance(vocabulary, dict):
      vocabs = vocabulary
    else:
      raise ValueError("vocabulary must be a dict, a Vocabulary, or None")

    dataset = tf.data.Dataset.from_tensor_slices(inputs)
    dataset = dataset.map(
        lambda x: {"inputs": tf.cast(vocabs["inputs"].encode_tf(x), tf.int64)},
        num_parallel_calls=t5.data.preprocessors.num_parallel_calls()
    )
    dataset = tokens_to_batches(
        dataset, sequence_length, batch_size, ["inputs"]
    )

    predictions = []
    for batch in dataset:
        predicted_tokens = self._model.generate(
                          input_ids=self.to_tensor(batch["inputs"]), **generate_kwargs
                      )
        predicted_tokens = predicted_tokens.cpu().numpy().tolist()
                      #print(predicted_tokens)
        predictions.extend(
                          [vocabs["targets"].decode(p) for p in predicted_tokens]
                      )   
    
    for inp, pred in zip(inputs, predictions):
      logging.info("%s\n  -> %s", inp, pred)

    if output_file is not None:
      write_lines_to_file(predictions, output_file)