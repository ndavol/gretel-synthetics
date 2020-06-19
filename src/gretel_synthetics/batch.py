"""
This module allows automatic splitting of a DataFrame
into smaller DataFrames (by clusters of columns) and doing
model training and text generation on each sub-DF independently.

Then we can concat each sub-DF back into one final synthetic dataset.

For example usage, please see our Jupyter Notebook.
"""
from dataclasses import dataclass, field
from pathlib import Path
import gzip
from typing import List, Type, Callable, Dict
import pickle
from copy import deepcopy
import logging
import io

import pandas as pd
import numpy as np
from tqdm.auto import tqdm

from gretel_synthetics.config import LocalConfig
from gretel_synthetics.generate import gen_text, generate_text
from gretel_synthetics.train import train_rnn


logging.basicConfig()
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


MAX_INVALID = 1000
FIELD_DELIM = "field_delimiter"
GEN_LINES = "gen_lines"


@dataclass
class Batch:
    """A representation of a synthetic data workflow.  It should not be used
    directly. This object is created automatically by the primary batch handler,
    such as ``DataFrameBatch``.  This class holds all of the necessary information
    for training, data generation and DataFrame re-assembly.
    """
    checkpoint_dir: str
    input_data_path: str
    headers: List[str]
    config: LocalConfig
    gen_data_count: int = 0

    training_df: Type[pd.DataFrame] = field(default_factory=lambda: None, init=False)
    gen_data_stream: io.StringIO = field(default_factory=io.StringIO, init=False)
    gen_data_invalid: List[gen_text] = field(default_factory=list, init=False)
    validator: Callable = field(default_factory=lambda: None, init=False)

    def __post_init__(self):
        self.reset_gen_data()

    @property
    def synthetic_df(self) -> pd.DataFrame:
        """Get a DataFrame constructed from the generated lines """
        if not self.gen_data_stream.getvalue():  # pragma: no cover
            return pd.DataFrame()
        self.gen_data_stream.seek(0)
        return pd.read_csv(self.gen_data_stream, sep=self.config.field_delimiter)

    def set_validator(self, fn: Callable, save=True):
        """Assign a validation callable to this batch. Optionally
        pickling and saving the validator for loading later
        """
        self.validator = fn
        if save:
            p = Path(self.checkpoint_dir) / "validator.p.gz"
            with gzip.open(p, "w") as fout:
                fout.write(pickle.dumps(fn))

    def load_validator_from_file(self):
        """Load a saved validation object if it exists """
        p = Path(self.checkpoint_dir) / "validator.p.gz"
        if p.exists():
            with gzip.open(p, "r") as fin:
                self.validator = pickle.loads(fin.read())

    def reset_gen_data(self):
        """Reset all objects that accumulate or track synthetic
        data generation
        """
        self.gen_data_invalid = []
        self.gen_data_stream = io.StringIO()
        self.gen_data_stream.write(
            self.config.field_delimiter.join(self.headers) + "\n"
        )
        self.gen_data_count = 0

    def add_valid_data(self, data: gen_text):
        """Take a ``gen_text`` object and add the generated
        line to the generated data stream
        """
        self.gen_data_stream.write(data.text + "\n")
        self.gen_data_count += 1

    def _basic_validator(self, raw_line: str):  # pragma: no cover
        return len(raw_line.split(self.config.field_delimiter)) == len(self.headers)

    def get_validator(self):
        """If a custom validator is set, we return that. Otherwise,
        we return the built-in validator, which simply checks if a generated
        line has the right number of values based on the number of headers
        for this batch.

        This at least makes sure the resulting DataFrame will be the right
        shape
        """
        if self.validator is not None:
            return self.validator

        return self._basic_validator


def _build_batch_dirs(
    base_ckpoint: str, headers: List[List[str]], config: dict
) -> dict:
    """Return a mapping of batch number => ``Batch`` object
    """
    out = {}
    logger.info("Creating directory structure for batch jobs...")
    base_path = Path(config["checkpoint_dir"])
    if not base_path.is_dir():
        base_path.mkdir()
    for i, headers in enumerate(headers):
        ckpoint = Path(base_ckpoint) / f"batch_{i}"
        if not ckpoint.is_dir():
            ckpoint.mkdir()
        checkpoint_dir = str(ckpoint)
        input_data_path = str(ckpoint / "train.csv")
        new_config = deepcopy(config)
        new_config.update(
            {"checkpoint_dir": checkpoint_dir, "input_data_path": input_data_path}
        )
        out[i] = Batch(
            checkpoint_dir=checkpoint_dir,
            input_data_path=input_data_path,
            headers=headers,
            config=LocalConfig(**new_config),
        )
        # try and load any previously saved validators
        out[i].load_validator_from_file()

    return out


class DataFrameBatch:
    """Create a multi-batch trainer / generator. When created, the directory
    structure to store models and training data will automatically be created.
    The directory structure will be created under the "checkpoint_dir" location
    provided in the ``config`` template. There will be one directory per batch,
    where each directory will be called "batch_N" where N is the batch number, starting
    from 0.

    Training and generating can happen per-batch or we can loop over all batches to
    do both train / generation functions.

    Example:
        When creating this object, you must explicitly create the training data
        from the input DataFrame before training models::

            my_batch = DataFrameBatch(df=my_df, config=my_config)
            my_batch.create_training_data()
            my_batch.train_all_batches()

    Args:
        df: The input, source DataFrame
        batch_size:  If ``batch_headers`` is not provided we automatically break up
            the number of colums in the source DataFrame into batches of N columns.
        batch_headers:  A list of lists of strings can be provided which will control
            the number of batches. The number of inner lists is the number of batches, and each
            inner list represents the columns that belong to that batch
        config: A template training config to use, this will be used as kwargs for each Batch's
            synthetic configuration.

    NOTE:
        When providing a config, the source of training data is not necessary, only the
        ``checkpoint_dir`` is needed. Each batch will control its input training data path
        after it creates the training dataset.
    """

    batches: Dict[int, Batch]
    """A mapping of ``Batch`` objects to a batch number. The batch number (key)
    increments from 0..N where N is the number of batches being used.
    """

    def __init__(
        self,
        *,
        df: pd.DataFrame,
        batch_size: int = 15,
        batch_headers: List[List[str]] = None,
        config: dict = None
    ):

        if not config:
            raise ValueError("config is required!")

        if not isinstance(df, pd.DataFrame):
            raise ValueError("df must be a Data Frame")

        if FIELD_DELIM not in config:
            raise ValueError("field_delimiter must be in config")

        if GEN_LINES not in config:
            config[GEN_LINES] = df.shape[0]

        self._source_df = df
        self.batch_size = batch_size
        self.config = config

        self._source_df.fillna("", inplace=True)

        self.master_header_list = list(self._source_df.columns)

        if not batch_headers:
            self.batch_headers = self._create_header_batches()
        else:
            self.batch_headers = batch_headers

        self.batches = _build_batch_dirs(
            self.config["checkpoint_dir"], self.batch_headers, self.config
        )

    def _create_header_batches(self):
        num_batches = len(self._source_df.columns) // self.batch_size
        tmp = np.array_split(list(self._source_df.columns), num_batches)
        return [list(row) for row in tmp]

    def create_training_data(self):
        """Split the original DataFrame into N smaller DataFrames. Each
        smaller DataFrame will have the same number of rows, but a subset
        of the columns from the original DataFrame.

        This method iterates over each ``Batch`` object and assigns
        a smaller training DataFrame to the ``training_df`` attribute
        of the object.

        Finally, a training CSV is written to disk in the specific
        batch directory
        """
        for i, batch in self.batches.items():
            logger.info(f"Generating training DF and CSV for batch {i}")
            out_df = self._source_df[batch.headers]
            batch.training_df = out_df.copy(deep=True)
            out_df.to_csv(
                batch.input_data_path,
                header=False,
                index=False,
                sep=self.config[FIELD_DELIM],
            )

    def train_batch(self, batch_idx: int):
        """Train a model for a single batch. All model information will
        be written into that batch's directory.

        Args:
            batch_idx: The index of the batch, from the ``batches`` dictionary
        """
        try:
            train_rnn(self.batches[batch_idx].config)
        except KeyError:
            raise ValueError("batch_idx is invalid")

    def train_all_batches(self):
        """Train a model for each batch.
        """
        for idx in self.batches.keys():
            self.train_batch(idx)

    def set_batch_validator(self, batch_idx: int, validator: Callable):
        """Set a validator for a specific batch. If a validator is configured
        for a batch, each generated record from that batch will be sent
        to the validator.

        Args:
            batch_idx: The batch number .
            validator: A callable that should take exactly one argument,
                which will be the raw line generated from the ``generate_text``
                function.
        """
        if not callable(validator):
            raise ValueError("validator must be callable!")
        try:
            self.batches[batch_idx].set_validator(validator)
        except KeyError:
            raise ValueError("invalid batch number!")

    def generate_batch_lines(self, batch_idx: int, max_invalid=MAX_INVALID):
        """Generate lines for a single batch. Lines generated are added
        to the underlying ``Batch`` object for each batch. The lines
        can be accessed after generation and re-assembled into a DataFrame.

        Args:
            batch_idx: The batch number
            max_invalid: The max number of invalid lines that can be generated, if
                this is exceeded, generation will stop
        """
        try:
            batch = self.batches[batch_idx]
        except KeyError:  # pragma: no cover
            raise ValueError("invalid batch index")
        batch: Batch
        batch.reset_gen_data()
        validator = batch.get_validator()
        t = tqdm(total=batch.config.gen_lines, desc="Valid record count ")
        t2 = tqdm(total=max_invalid, desc="Invalid record count ")
        line: gen_text
        for line in generate_text(
            batch.config, line_validator=validator, max_invalid=max_invalid
        ):
            if line.valid is None or line.valid is True:
                batch.add_valid_data(line)
                t.update(1)
            else:
                t2.update(1)
                batch.gen_data_invalid.append(line)
        t.close()
        t2.close()
        return batch.gen_data_count == batch.config.gen_lines

    def generate_all_batch_lines(self, max_invalid=MAX_INVALID) -> dict:
        """Generate synthetic lines for all batches. Lines for each batch
        are added to the individual ``Batch`` objects. Once generateion is
        done, you may re-assemble the dataset into a DataFrame.

        Example::

            my_batch.generate_all_batch_lines()
            # Wait for all generation to complete
            synthetic_df = my_batch.batches_to_df()

        Args:
            max_invalid: The number of invalid lines, per batch. If this number
                is exceeded for any batch, generation will stop.

        Returns:
            A dictionary of batch number to a bool value that shows if each batch
            was able to generate the full number of requested lines::

                {
                    0: True,
                    1: True
                }
        """
        batch_status = {}
        for idx in self.batches.keys():
            batch_status[idx] = self.generate_batch_lines(idx, max_invalid=max_invalid)
        return batch_status

    def batch_to_df(self, batch_idx: int) -> pd.DataFrame:  # pragma: no cover
        """Extract a synthetic data DataFrame from a single batch.

        Args:
            batch_idx: The batch number

        Returns:
            A DataFrame with synthetic data
        """
        try:
            return self.batches[batch_idx].synthetic_df
        except KeyError:
            raise ValueError("batch_idx is invalid!")

    def batches_to_df(self) -> pd.DataFrame:
        """Convert all batches to a single synthetic data DataFrame.

        Returns:
            A single DataFrame that is the concatenation of all the
            batch DataFrames.
        """
        batch_iter = iter(self.batches.values())
        base_batch = next(batch_iter)
        accum_df = base_batch.synthetic_df

        for batch in batch_iter:
            accum_df = pd.concat([accum_df, batch.synthetic_df], axis=1)

        return accum_df[self.master_header_list]