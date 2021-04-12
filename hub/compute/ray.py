import sys
from hub import Dataset
from hub.api.datasetview import DatasetView
from hub.utils import batchify
from hub.compute import Transform
from typing import Iterable, Iterator
from hub.exceptions import ModuleNotInstalledException
from hub.api.sharded_datasetview import ShardedDatasetView
import hub
from hub.api.dataset_utils import get_value, str_to_int
from hub.schema.features import featurify
import numpy as np
import math
import pickle
from collections import defaultdict
from tqdm import tqdm


def get_sample_size(schema, workers, memory_per_worker):
    """Given Schema, decides how many samples to take at once and returns it"""
    schema = featurify(schema)
    size_sum = 0
    # size_dict = {}
    for feature in schema._flatten():
        shp = list(feature.max_shape)
        if len(shp) == 0:
            shp = [1]

        sz = np.dtype(feature.dtype).itemsize
        if feature.dtype == "object":
            sz = (16 * 1024 * 1024 * 8) / 128

        def prod(shp):
            res = 1
            for s in shp:
                res *= s
            return res

        size = prod(shp) * sz
        size_sum += size
        # size_dict[feature.path[1:]] = size
    samples = memory_per_worker * 1024 * 1024 / size_sum
    samples = 2 ** math.floor(math.log2(samples))
    samples = samples * workers
    return samples


def empty_remote(template, **kwargs):
    """
    remote template
    """

    def wrapper(func):
        def inner(**kwargs):
            return func

        return inner

    return wrapper


try:
    import ray

    remote = ray.remote
except Exception:
    remote = empty_remote


class RayTransform(Transform):
    def __init__(self, func, schema, ds, scheduler="ray", workers=1, **kwargs):
        super(RayTransform, self).__init__(
            func, schema, ds, scheduler="single", workers=workers, **kwargs
        )
        self.workers = workers
        if "ray" not in sys.modules:
            raise ModuleNotInstalledException("ray")

        if not ray.is_initialized():
            ray.init(local_mode=True)

    @remote
    def transform_upload_shard(
        transform_obj, ds_in, ds_out, worker_id, batch, num_workers, n_samples
    ):
        def _func_argd(item):
            if isinstance(item, (DatasetView, Dataset)):
                item = item.numpy()
            result = transform_obj.call_func(
                0, item
            )  # If the iterable obtained from iterating ds_in is a list, it is not treated as list
            if not isinstance(result, list):
                result = [result]
            assert len(result) == 1 and type(result[0]) == dict
            return transform_obj._flatten_dict(result[0])

        # indexes = [idx for idx in range(batch * n_samples)]
        shard_size = math.ceil(len(ds_in) / num_workers)
        start_idx = worker_id * shard_size
        end_idx = min(start_idx + shard_size, len(ds_in))
        # worker_id = indexes[0] // shard_size
        results = [_func_argd(ds_in[idx]) for idx in range(start_idx, end_idx)]
        start_idx += batch * n_samples
        end_idx += batch * n_samples
        results = transform_obj._split_list_to_dicts(results)
        temp_dict = {}
        value_shape = {}
        temp_file = None

        for key, value in results.items():
            value = get_value(value)
            value = str_to_int(value, ds_out.tokenizer)
            if len(value) >= ds_out[key, 0].chunksize[0]:
                # start_idx = batch * n_samples + worker_id * shard_size
                # end_idx = start_idx + len(value)
                print(f"*** start writing {key} ****")
                tensor = ds_out._tensors[f"/{key}"]

                if tensor.is_dynamic:
                    tensor.disable_dynamicness()
                    shape = tensor.get_shape_from_value(
                        [slice(start_idx, end_idx)], value
                    )
                else:
                    shape = None

                ds_out[
                    key,
                    start_idx:end_idx,
                ] = value

                value_shape[key] = (start_idx, end_idx, shape)
                print(f"*** done writing {key} ****")
            else:
                print(f"*** start temping {key} ****")
                temp_file = f"tmp/worker_{worker_id}"
                temp_dict[key] = value
                print(f"*** done temping {key} ****")
        if temp_file:
            ds_out._fs_map[temp_file] = pickle.dumps(temp_dict)
            ds_out.flush()
        return value_shape, temp_file

    def store(
        self,
        url: str,
        token: dict = None,
        length: int = None,
        ds: Iterable = None,
        progressbar: bool = True,
        public: bool = True,
        memory_per_worker=128,
    ):
        """
        The function to apply the transformation for each element in batchified manner

        Parameters
        ----------
        url: str
            path where the data is going to be stored
        token: str or dict, optional
            If url is refering to a place where authorization is required,
            token is the parameter to pass the credentials, it can be filepath or dict
        length: int
            in case shape is None, user can provide length
        ds: Iterable
        progressbar: bool
            Show progress bar
        public: bool, optional
            only applicable if using hub storage, ignored otherwise
            setting this to False allows only the user who created it to access the dataset and
            the dataset won't be visible in the visualizer to the public

        Returns
        ----------
        ds: hub.Dataset
            uploaded dataset
        """
        _ds = ds or self.base_ds
        n_samples = get_sample_size(self.schema, self.workers, memory_per_worker)
        ds_out = self.create_dataset(url, length=len(_ds), token=token, public=public)

        def batchify_generator(iterator: Iterable, size: int):
            batch = []
            for el in iterator:
                batch.append(el)
                if len(batch) >= size:
                    yield batch
                    batch = []
            yield batch

        def write_dynamic_shapes(value_shapes_list):
            for value_shapes in value_shapes_list:
                for key, value in value_shapes.items():
                    tensor = ds_out._tensors[f"/{key}"]
                    if tensor.is_dynamic:
                        tensor.enable_dynamicness()
                        start_idx, end_idx, value_shape = value
                        tensor.set_dynamic_shape(
                            [slice(start_idx, end_idx)], value_shape
                        )

        def write_temp_chunks(fs_map, temp_files):
            combined_temp_dict = defaultdict(list)
            print("*** reading, deleting chunks ****")
            for temp_file in temp_files:
                if temp_file:
                    temp_dict = pickle.loads(fs_map[temp_file])
                    for key, value in temp_dict.items():
                        combined_temp_dict[key].extend(value)
                    del fs_map[temp_file]
            print("*** done reading, deleting ****")
            print("*** writing chunks ****")
            # write the chunks from temp files
            for key, value in combined_temp_dict.items():
                print(f"*** writing {key}****")
                start_idx = batch * n_samples
                end_idx = start_idx + len(value)
                ds_out._tensors[f"/{key}"].enable_dynamicness()
                ds_out[
                    key,
                    start_idx:end_idx,
                ] = value

            print("*** done writing chunks ****")

        with tqdm(
            total=len(_ds),
            unit_scale=True,
            unit=" items",
            desc=f"Storing in batches of size {n_samples}",
        ) as pbar:
            for batch, ds_in_batch in enumerate(batchify_generator(_ds, n_samples)):
                # iterator = ray.util.iter.from_range(
                #     len(ds_in_batch), num_shards=self.workers
                # )
                work = [
                    self.transform_upload_shard.remote(
                        self,
                        ds_in_batch,
                        ds_out,
                        worker,
                        batch,
                        self.workers,
                        n_samples,
                    )
                    for worker in range(self.workers)
                ]
                print("*** Doing work ****")
                value_shapes_list, temp_files = zip(*ray.get(work))
                print("*** Work done****")
                # write dynamic_shapes
                print("*** Writing dynamic_shapes ****")
                write_dynamic_shapes(value_shapes_list)
                print("*** Done writing dynamic_shapes ****")

                print("*** Writing temp chunks****")
                fs_map = ds_out._fs_map

                write_temp_chunks(fs_map, temp_files)
                print("*** Done writing temp chunks ****")

                pbar.update(len(ds_in_batch))
        ds_out.flush()
        return ds_out
