from ..io.dataset import KeypointDataset, FeatureDataset, Dataset
from ..project.paths import Project
from ..config import load_calibration_data, save_calibration_data
from ..pca import fit_with_center, CenteredPCA

import jax.numpy as jnp
import jax.random as jr
import jax.numpy.linalg as jla
import matplotlib.pyplot as plt
import logging


class reducer(object):
    type_name = None
    defaults = dict()

    @classmethod
    def reduce(cls: type, dataset: KeypointDataset, config) -> FeatureDataset:
        """Reduce a dataset to a set of features.

        Only valid for alignment method 'locked_pts'.

        Parameters
        ----------
        dataset : FeatureDataset
        config : dict
            `features` section of config file.
        """
        return dataset.as_features().with_data(
            cls.reduce_array(dataset.data, config)
        )

    @classmethod
    def inflate(cls, dataset: FeatureDataset, config: dict):
        """Reconstruct a dataset from a set of features."""
        # reinflate removed axes as zeros
        inflated = dataset.with_data(cls.inflate_array(dataset.data, config))
        return inflated.as_keypoints(config["calibration_data"]["kpt_names"])

    @staticmethod
    def reduce_array(arr, config):
        """Reduce an array of keypoints to an array of features.

        Only valid for alignment method 'locked_pts'.

        Parameters
        ----------
        arr : jnp.ndarray (n_samples, ...)
        config : dict
            `features` section of config file.
        """
        raise NotImplementedError

    @staticmethod
    def inflate_array(arr, config):
        """Reconstruct an array of keypoints from an array of features.

        arr : array, shape (n_samples, n_features) or (n_features,)
            Array of reduced features.
        """
        raise NotImplementedError

    @classmethod
    def calibrate(cls, dataset: KeypointDataset, config, **kwargs):
        """Calibrate feature extraction for a dataset.

        Parameters
        ----------
        dataset : KeypointDataset
        config : dict
            Full config
        """
        calib = config["features"]["calibration_data"] = dict(
            kpt_names=dataset.keypoint_names
        )
        calib.update(cls._calibrate(dataset, config, **kwargs))
        return config

    @staticmethod
    def _calibrate(dataset: KeypointDataset, config):
        """Calibrate feature extraction for a dataset."""
        raise NotImplementedError

    @staticmethod
    def plot_calibration(project: Project, config: dict, colors=None):
        """Plot calibration data."""
        raise NotImplementedError

    @staticmethod
    def _validate_config(config):
        raise NotImplementedError


class locked_pts(reducer):
    type_name = "locked_pts"
    defaults = dict(
        reduce_ixs=None,
    )

    @staticmethod
    def reduce_array(arr, config):
        """Reduce an array of keypoints to an array of features by removing
        dimensions marked in config"""
        flat_data = arr.reshape((arr.shape[0], -1))
        ix_mask = jnp.isin(
            jnp.arange(flat_data.shape[-1]),
            jnp.array(config["calibration_data"]["reduce_ixs"]),
        )
        return flat_data[..., ~ix_mask]

    @staticmethod
    def inflate_array(arr, config):
        """Reconstruct an array of keypoints from an array of features by
        inserting dimensions."""
        calib = config["calibration_data"]
        inflated_shape = (arr.shape[0], calib["n_kpts"], -1)
        # if passed a single frame
        if arr.ndim < 2:
            arr = arr[None]
            inflated_shape = (calib["n_kpts"], -1)
        # add indices that were dropped
        for reduced_ix in sorted(calib["reduce_ixs"]):
            arr = jnp.insert(arr, reduced_ix, 0, axis=-1)
        # return, reshaped to number of keypoints
        return arr.reshape(inflated_shape)

    @staticmethod
    def _calibrate(dataset: KeypointDataset, config: dict):
        """Calibrate feature extraction for a dataset."""
        # find keypoints with zero std
        flat_data = dataset.as_features()
        reduce_ixs = jnp.where(flat_data.data.std(axis=0) < 1e-5)[0].tolist()

        return dict(
            reduce_ixs=reduce_ixs,
            n_kpts=dataset.n_points,
        )

    @staticmethod
    def plot_calibration(project: Project, config: dict, colors=None):
        """Plot calibration data."""
        return None

    @staticmethod
    def _validate_config(config):
        assert (
            config.get("reduce_ixs") is not None
        ), "Feature reduction not calibrated."


class pcs(reducer):
    """Feature reduction based on principal components of a full dataset."""

    type_name = "pcs"
    defaults = dict(
        calibration=dict(tgt_variance=0.98),
        max_pts=10000,
        subset_seed=823,
    )

    @staticmethod
    def reduce_array(arr, config):
        """Reduce an array of keypoints to an array of features."""
        flat_data = arr.reshape((arr.shape[0], -1))
        calib = config["calibration_data"]
        pcs = CenteredPCA(calib["center"], calib["pcs"])
        return pcs.coords(flat_data)[..., : calib["n_dims"]]

    @staticmethod
    def inflate_array(arr, config):
        """Reconstruct an array of keypoints from an array of features."""
        calib = config["calibration_data"]
        pcs = CenteredPCA(calib["center"], calib["pcs"])
        # add zeros for any dimensions that were removed
        n_pcs = pcs._pcadata.s.shape[0]
        zeroes = jnp.zeros(arr.shape[:-1] + (n_pcs - calib["n_dims"],))
        flat_data = jnp.concatenate([arr, zeroes], axis=-1)
        # transform out of PC space
        flat_arr = pcs.from_coords(flat_data)
        kpt_shape = (calib["n_kpts"], flat_arr.shape[-1] // calib["n_kpts"])
        return flat_arr.reshape(arr.shape[:-1] + kpt_shape)

    @classmethod
    def calibrate(cls, dataset: KeypointDataset, config, n_dims=None):
        """Calibrate feature extraction for a dataset.

        Parameters
        ----------
        dataset : KeypointDataset
        config : dict
            Full config
        n_dims : int
            Number of dimensions to select, or None to choose a number of
            dimensions explaining `tgt_variance` of the variance as specified in
            the config.
        """
        return super().calibrate(dataset, config, n_dims=n_dims)

    @staticmethod
    def _calibrate(dataset: KeypointDataset, config: dict, n_dims=None):
        """Calibrate feature extraction for a dataset."""
        config = config["features"]

        flat_data = dataset.as_features().data
        if config["max_pts"] is not None:
            subset = jr.choice(
                jr.PRNGKey(config["subset_seed"]),
                flat_data.shape[0],
                (config["max_pts"],),
                replace=False,
            )
            logging.info(
                f"Reducing dataset size for PCA: {flat_data.shape[0]} "
                f"to {config['max_pts']}."
            )
            flat_data = flat_data[subset]

        # fit PCA
        pcs = fit_with_center(flat_data)

        # -- choose number of dimensions in reduced data
        scree = (
            jnp.cumsum(pcs._pcadata.variances())
            / pcs._pcadata.variances().sum()
        )
        if n_dims is None:
            selected_ix = jnp.argmax(
                scree > config["calibration"]["tgt_variance"]
            )
        else:
            selected_ix = n_dims - 1

        # -- keypoint errors resulting from PCA reduction
        flat_arr = flat_data - pcs._center[None]
        coords = pcs._pcadata.coords(flat_arr)
        # (n_samples, n_dims, n_dims), sum over axis 1 (aka -2) gives flat_data
        reconst_parts = coords[..., None] * pcs._pcadata.pcs()[None, ...]
        cum_reconst = jnp.cumsum(reconst_parts, axis=-2)
        kpt_shape = (
            dataset.n_points,
            flat_arr.shape[-1] // dataset.n_points,
        )
        cum_dists = cum_reconst - flat_arr[..., None, :]
        errs = jla.norm(cum_dists.reshape(flat_arr.shape + kpt_shape), axis=-1)

        # outputs to main config and calibration_data
        return dict(
            pcs=pcs._pcadata,
            center=pcs._center,
            mean_errs=errs.mean(axis=0),
            n_kpts=dataset.n_points,
            n_dims=int(selected_ix) + 1,
        )

    @staticmethod
    def plot_calibration(config: dict, colors=None):
        """Plot calibration data."""
        from ..viz.general import scree

        config = config["features"]
        calib = config["calibration_data"]
        pcs = CenteredPCA(calib["center"], calib["pcs"])
        cumsum = (
            jnp.cumsum(pcs._pcadata.variances())
            / pcs._pcadata.variances().sum()
        )

        fig, ax = plt.subplots(1, 2, figsize=(6, 2.0))

        # variance explained
        scree(
            cumsum,
            calib["n_dims"],
            config["calibration"]["tgt_variance"],
            ax=ax[0],
        )
        ax[0].set_title("Feature reduction: PCA scree")

        # variance explained
        scree(
            calib["mean_errs"],
            calib["n_dims"],
            None,
            ax=ax[1],
        )
        ax[1].set_title("Keypoint reconstruction")
        ax[1].set_ylabel("Euclidean dist.")
        return fig

    @staticmethod
    def _validate_config(config):
        if config.get("n_components") is None:
            raise ValueError("n_components must be set for PCA features.")
        if config.get("n_kpts") is None:
            raise ValueError("n_kpts must be set for PCA features.")


feature_types = {
    locked_pts.type_name: locked_pts,
    pcs.type_name: pcs,
}
default_feature_type = "locked_pts"


def reduce_to_features(dataset: KeypointDataset, config: dict):
    """Reduce keypoint dataset to non-singular features.

    dataset : Dataset
        Dataset whose `ndim` is compatible with the alignment type specified in
        `config`.
    config : dict
        `features` section of config file.
    """
    feat_type = config["type"]
    if feat_type not in feature_types:
        raise NotImplementedError(f"Unknown dataset type: {feat_type}")
    if hasattr(dataset, "_is_dataset"):
        return feature_types[feat_type].reduce(dataset, config)
    else:
        return feature_types[feat_type].reduce_array(dataset, config)


def inflate(dataset, config):
    """Reconstruct a dataset from a set of features."""
    feat_type = config["type"]
    if feat_type not in feature_types:
        raise NotImplementedError(f"Unknown dataset type: {feat_type}")
    if hasattr(dataset, "_is_dataset"):
        return feature_types[feat_type].inflate(dataset, config)
    else:
        return feature_types[feat_type].inflate_array(dataset, config)
