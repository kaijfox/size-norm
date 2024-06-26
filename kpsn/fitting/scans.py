from ..config import (
    loads,
    load_config,
    flatten,
    deepen,
    save_config,
    load_model_config,
    save_model_config,
)
from ..project.paths import ensure_dirs, Project, recursive_update, create_model
from .methods import (
    fit_types,
    fit,
    prepare_dataset,
)
from ..io.loaders import load_dataset

from ..io.features import inflate
from ..io.dataset_refactor import Dataset
from ..io.utils import split_body_inv
from ..logging import ArrayTrace
from ..models.joint import JointModelParams, JointModel
from ..models.instantiation import get_model
from ..models.util import (
    apply_bodies,
    reconst_errs,
    induced_reference_keypoints,
    _optional_pbar,
)
from ..clouds import PointCloudDensity, ball_cloud_js
from .methods import load_fit

import jax.tree_util as pt
from typing import Tuple, Union
from os import PathLike
from pathlib import Path
import jax.numpy as jnp
import logging
import shutil
import tqdm


def setup_scan_config(
    project: Project,
    name: str,
    scan_params: dict,
    scan_config_overrides: dict = {},
    model_name_fmt: str = "{scan_name}_{i}",
    model_overrides: dict = {},
):
    """Setup a config for a parameter scan.

    Parameters
    ----------
    config_path : pathlib.Path
        Path to config file.
    scan_params : dict
        Dictionary mapping parameter to a list of values. May be nested or
        flattened, i.e. `fit.n_steps`.
    """

    # preprocess args
    if isinstance(model_name_fmt, str):
        name_fmt_string = model_name_fmt
        model_name_fmt = lambda **kwargs: name_fmt_string.format(**kwargs)
    scan_params = flatten(scan_params)
    n_models = len(scan_params[list(scan_params.keys())[0]])

    # Fill out main scan config
    config = scan_cfg_structure.copy()
    config["models"] = {
        model_name_fmt(scan_name=name, i=i): {
            p_name: p_vals[i] for p_name, p_vals in scan_params.items()
        }
        for i in range(n_models)
    }

    # set up model config modified to run with `split` fit method
    model_config = load_model_config(project.base_model_config())
    model_config = recursive_update(model_config, deepen(model_overrides))
    if model_config["fit"]["type"] != "standard":
        raise ValueError("Scan only supports standard fit type for base model.")
    scan_config = recursive_update(
        fit_types["split"].defaults, deepen(scan_config_overrides)
    )
    model_config["fit"] = {
        "type": "split",
        **scan_config,
        **{"em": model_config["fit"]},
    }

    # create scan directory and save configs
    ensure_dirs(project)
    scan_dir = project.scan(name)
    scan_dir.mkdir(exist_ok=True)
    save_config(scan_dir / "scan.yml", config)
    save_model_config(scan_dir / "base_model.yml", model_config)
    return config, model_config


def run_scan(
    project: Project,
    scan_name: str,
    dataset: Dataset = None,
    checkpoint_every: int = 10,
    log_every: int = -1,
    progress: bool = False,
    force_restart: bool = False,
):
    """Load configs and run the desired scan."""
    scan_config = load_config(project.scan(scan_name) / "scan.yml")
    model_config = load_model_config(project.scan(scan_name) / "base_model.yml")
    if dataset is None:
        dataset = load_dataset(model_config["dataset"])
    for model_name, scan_params in scan_config["models"].items():
        if force_restart:
            model_dir = project.model(model_name)
            if model_dir.exists():
                logging.info(f"Removing existing model {model_dir}")
                shutil.rmtree(model_dir)
        model_dir, model_cfg = create_model(
            project,
            model_name,
            config=model_config,
            config_overrides=scan_params,
        )
        fit(model_dir, dataset, checkpoint_every, log_every, progress)


scan_cfg_structure = loads(
    """\
    models: null"""
)


def _resolve_model_config(
    project_or_config: Project, scan_name=None, model_name=None
):
    """
    Resolve a model config from a project, scan, or model name.

    Parameters
    ----------
    project_or_config : Project, str or PathLike, dict
        Project from which configs should be loaded, or path to a config file,
        or the config itself.
    scan_name : str
        Scan to load dataset for. Optional if config or path to config is
        provided, or if a model_name is provided.
    model_name : str
        Model to load dataset for. Optional if config or path to config is
        provided, or if a scan_name is provided."""
    project = project_or_config
    if isinstance(project, dict):
        cfg = project
    else:
        if scan_name is not None:
            config_path = project.scan(scan_name) / "base_model.yml"
        elif model_name is not None:
            config_path = project.model_config(model_name)
        else:
            model_path = Path(project)
            config_path = model_path / "model.yml"
        cfg = load_model_config(config_path)
    return cfg


def load_scan_dataset(
    project_or_config: Project,
    scan_name=None,
    model_name=None,
    allow_subsample=True,
) -> Tuple[Dataset, dict]:
    """Load modified dataset for/from a scan.

    Parameters
    ----------
    project_or_config : Project, str or PathLike, dict
        Project from which configs should be loaded, or path to a config file,
        or the config itself.
    scan_name : str
        Scan to load dataset for. Optional if config or path to config is
        provided, or if a model_name is provided.
    model_name : str
        Model to load dataset for. Optional if config or path to config is
        provided, or if a scan_name is provided.
    allow_subsample : bool
        Allow subsampling of the dataset when loading.

    Returns
    -------
    dataset : Dataset
    config : dict
        Full model and project config."""

    cfg = _resolve_model_config(project_or_config, scan_name, model_name)
    return load_dataset(cfg["dataset"], allow_subsample=allow_subsample), cfg


def prepare_scan_dataset(
    dataset: Dataset,
    project_or_config: Union[Project, dict, str, PathLike],
    scan_name: str = None,
    model_name: str = None,
    all_versions: bool = False,
    return_session_inv: bool = False,
):
    """Prepare a loaded dataset for a scan.

    Parameters
    ----------
    dataset : Dataset
        Loaded (unprepared) dataset.
    config : dict
        Full model and project config.
    all_versions : bool
        Return all versions (raw, aligned, feature-extracted) of the dataset.
    return_session_inv : bool
        Return the session mapping used to create the split sessions as part of
        split_metadata.

    Returns
    -------
    prepped : Dataset
    split_meta : dict
        Mapping from bodies in the original dataset to sessions in the prepared
        dataset. If `return_session_inv` is True, a tuple of this mapping and
        one from original dataset session names to the split dataset sessions
        deriving from them.
    align_inv : dict
        Data required to invert alignment.
    """
    config = _resolve_model_config(project_or_config, scan_name, model_name)
    prepped, align_inv = prepare_dataset(
        dataset, config, all_versions=all_versions
    )

    # map from original dataset bodies to sessions in the split dataset
    _body_inv, _session_inv = split_body_inv(
        dataset,
        config["fit"]["split_all"],
        config["fit"]["split_type"],
        config["fit"]["split_count"],
    )

    if return_session_inv:
        return prepped, (_body_inv, _session_inv), align_inv
    return prepped, _body_inv, align_inv


def load_and_prepare_scan_dataset(
    project,
    scan_name=None,
    model_name=None,
    all_versions=False,
    return_session_inv=False,
    allow_subsample=True,
):
    """Load and prepare a dataset for a scan.

    Parameters
    ----------
    project : Project, str or PathLike, dict
        Project from which configs should be loaded, or path to a config file,
        or the config itself.
    scan_name : str
        Scan to load dataset for. Optional if config or path to config is
        provided, or if a model_name is provided.
    model_name : str
        Model to load dataset for. Optional if config or path to config is
        provided, or if a scan_name is provided.
    all_versions : bool
        Return all versions of the dataset.
    return_session_inv : bool
        Return the session mapping used to create the split sessions as part of
        split_metadata.
    allow_subsample : bool
        Allow subsampling of the dataset when loading.

    Returns
    -------
    dataset : Dataset
    split_meta : dict
        Mapping from bodies in the original dataset to sessions in the prepared
        dataset. If `return_session_inv` is True, a tuple of this mapping and
        one from original dataset session names to the split dataset sessions
        deriving from them.
    config : dict
    """

    dataset, cfg = load_scan_dataset(
        project, scan_name, model_name, allow_subsample
    )
    return prepare_scan_dataset(
        dataset, cfg, all_versions, return_session_inv
    ) + (cfg,)


def model_withinbody_reconst_errs(
    project,
    model_name,
    dataset=None,
    _body_inv=None,
    _inflate=None,
    progress=False,
):
    """Keypoint errors induced by morphing across examples of the same body
    in a split-dataset scan."""

    # for each body in the dataset, calculate the reconstruction error
    # after morphing to a within-body reference session of the model that
    # did not know these bodies should be identical

    checkpoint = load_fit(project.model(model_name))
    cfg = load_model_config(project.model_config(model_name))
    if dataset is None:
        dataset, _body_inv, _ = load_and_prepare_scan_dataset(cfg)
        _inflate = lambda x: inflate(x, cfg["features"])
    model = get_model(cfg)

    # select session/body for canonical pose space
    global_ref_body = dataset.sess_bodies[dataset.ref_session]

    errs = {}
    pbar = _optional_pbar(_body_inv[b], progress)
    for b in pbar:
        nonref_sessions = _body_inv[b][1:]
        ref_body = dataset.sess_bodies[_body_inv[b][0]]
        # nonref sessions mapped to canonical pose space
        subset = dataset.session_subset(nonref_sessions, bad_ref_ok=True)
        mapped_split_body = apply_bodies(
            model.morph,
            checkpoint["params"].morph,
            subset,
            {s: global_ref_body for s in nonref_sessions},
        )
        mapped_split_body = _inflate(mapped_split_body)
        # pretend all nonref sessions have body `ref_body`, mapped to canonical pose space
        with_ref_body = subset.with_sess_bodies(
            {s: ref_body for s in nonref_sessions}
        )
        mapped_ref_body = apply_bodies(
            model.morph,
            checkpoint["params"].morph,
            with_ref_body,
            {s: global_ref_body for s in nonref_sessions},
        )
        mapped_ref_body = _inflate(mapped_ref_body)

        errs[b] = {
            s: reconst_errs(
                mapped_ref_body.get_session(s), mapped_split_body.get_session(s)
            )
            for s in nonref_sessions
        }

    return errs


def model_withinbody_induced_errs(
    project,
    model_name,
    dataset=None,
    _body_inv=None,
):
    """Keypoint errors induced by morphing across examples of the same body
    in a split-dataset scan."""

    # for each body in the dataset, calculate the reconstruction error
    # after morphing to a within-body reference session of the model that
    # did not know these bodies should be identical

    checkpoint = load_fit(project.model(model_name))
    cfg = checkpoint["config"]
    if dataset is None:
        dataset, _body_inv, _ = load_and_prepare_scan_dataset(cfg)
        _inflate = lambda x: inflate(x, cfg["features"])
    model = get_model(cfg)

    induced_kpts = induced_reference_keypoints(
        dataset,
        cfg,
        model.morph,
        checkpoint["params"].morph,
        to_body=None,  # map to all bodies
        include_reference=True,
    )

    errs = {}
    for b in _body_inv:
        # _body_inv: map (pre-split) body to sessions with that body
        # Now map sessions in _body_inv[b] to their (post-split) body name
        # Also separate out into a reference session within _body_inv[b] (the
        # first) entry and the other sessions
        body_ref = dataset.session_body_name(_body_inv[b][0])
        nonref_sessions = _body_inv[b][1:]
        nonref_bodies = [dataset.session_body_name(s) for s in nonref_sessions]
        # induced_kpts is indexed by (post-split) body names
        # measure errors between the reference session for this (pre-split)
        # body, that is `body_ref` and each of the non-reference sessions
        errs[b] = {
            s: reconst_errs(induced_kpts[b], induced_kpts[body_ref])
            for b, s in zip(nonref_bodies, nonref_sessions)
        }
    return errs


def withinbody_reconst_errs(project, scan_name, progress=False):
    """Keypoint errors induced by morphing across examples of the same body for
    each model in a scan."""
    if isinstance(scan_name, str):
        scan_cfg = load_config(project.scan(scan_name) / "scan.yml")
        models = list(scan_cfg["models"].keys())
    else:
        models = scan_name
    dataset, _body_inv, cfg = load_and_prepare_scan_dataset(
        project, model_name=models[0]
    )
    _inflate = lambda x: inflate(x, cfg["features"])
    return {
        model: model_withinbody_reconst_errs(
            project,
            model,
            dataset=dataset,
            _body_inv=_body_inv,
            _inflate=_inflate,
            progress=model if progress else False,
        )
        for model in models
    }


def _resolve_scan_model_list(project, scan_name):
    if isinstance(scan_name, str):
        scan_cfg = load_config(project.scan(scan_name) / "scan.yml")
        return list(scan_cfg["models"].keys())
    return scan_name


def _load_dataset_or_calc_metadata(
    project, model_name, dataset=None, split_meta=None
):
    # load dataset if not given, or get split metadata if dataset was given
    if dataset is None:
        dataset, (_body_inv, _session_inv), _ = load_and_prepare_scan_dataset(
            project, model_name=model_name, return_session_inv=True
        )
    else:
        _body_inv, _session_inv = split_meta
    return dataset, _body_inv, _session_inv


def withinbody_induced_errs(
    project, scan_name, dataset=None, split_meta=None, progress=False
):
    """Keypoint errors induced by morphing across examples of the same body for
    each model in a scan."""
    models = _resolve_scan_model_list(project, scan_name)
    dataset, _body_inv, _ = _load_dataset_or_calc_metadata(
        project, models[0], dataset, split_meta
    )

    return {
        model: model_withinbody_induced_errs(
            project,
            model,
            dataset=dataset,
            _body_inv=_body_inv,
        )
        for model in _optional_pbar(models, progress)
    }


def withinsession_induced_errs(
    project, scan_name, dataset=None, split_meta=None, progress=False
):
    """Keypoint errors induced by morphing across examples of the same session
    for each model in a scan with split_all = True."""
    models = _resolve_scan_model_list(project, scan_name)
    # load dataset if not given, or get split metadata if dataset was given
    dataset, _body_inv, _session_inv = _load_dataset_or_calc_metadata(
        project,
        models[0],
        dataset,
        split_meta,
    )

    # calculate induced errors for each model
    return _body_inv, {
        model: model_withinbody_induced_errs(
            project,
            model,
            dataset=dataset,
            _body_inv=_session_inv,
        )
        for model in _optional_pbar(models, progress)
    }


def base_jsds_to_reference(
    project,
    model_name=None,
    dataset=None,
    _body_inv=None,
    ref_cloud=None,
    progress=False,
):
    """
    Compute JSDs to reference session for each body in the dataset.
    """

    assert (
        model_name is not None or dataset is not None
    ), "Need either `model_name` or `dataset`."
    if dataset is None:
        dataset, _body_inv, _ = load_and_prepare_scan_dataset(
            project, model_name=model_name
        )
        ref_cloud = PointCloudDensity(k=15).fit(
            dataset.get_session(dataset.ref_session)
        )

    # transform all sessions to the global reference session's body
    pbar = _optional_pbar(_body_inv, progress)
    jsds = {
        b: {
            s: ball_cloud_js(
                ref_cloud,
                PointCloudDensity(k=15).fit(dataset.get_session(s)),
            )
            for s in _body_inv[b]
        }
        for b in pbar
    }

    return jsds


def model_jsds_to_reference(
    project,
    model_name,
    dataset=None,
    _body_inv=None,
    ref_cloud=None,
    progress=False,
):
    """JS discances of each session (after normalization) to the reference
    session."""
    cfg = load_model_config(project.model_config(model_name))
    if dataset is None:
        dataset, _body_inv, _ = load_and_prepare_scan_dataset(
            project, model_name=model_name
        )
        ref_cloud = PointCloudDensity(k=15).fit(
            dataset.get_session(dataset.ref_session)
        )
    model = get_model(cfg)
    checkpoint = load_fit(project.model(model_name))

    induced_kpts = induced_reference_keypoints(
        dataset,
        cfg,
        model.morph,
        checkpoint["params"].morph,
        to_body=None,  # map to all bodies
        include_reference=True,
        return_features=True,
    )

    # transform all sessions to the global reference session's body
    jsds = {}
    for b in _optional_pbar(_body_inv, progress):

        # compute JS distances
        jsds[b] = {
            s: ball_cloud_js(
                ref_cloud,
                PointCloudDensity(k=15).fit(
                    induced_kpts[dataset.session_body_name(s)]
                ),
            )
            for s in _body_inv[b]
        }

    return jsds


def jsds_to_reference(
    project, scan_name, dataset=None, split_meta=None, progress=False
):
    """JS distances to reference session for each model in a scan."""
    models = _resolve_scan_model_list(project, scan_name)
    dataset, _body_inv, _ = _load_dataset_or_calc_metadata(
        project,
        models[0],
        dataset,
        split_meta,
    )

    ref_cloud = PointCloudDensity(k=15).fit(
        dataset.get_session(dataset.ref_session)
    )
    model_jsds = {
        model: model_jsds_to_reference(
            project,
            model,
            dataset=dataset,
            _body_inv=_body_inv,
            ref_cloud=ref_cloud,
            progress=str(model) if progress else False,
        )
        for model in models
    }
    base_jsds = base_jsds_to_reference(
        project,
        dataset=dataset,
        _body_inv=_body_inv,
        ref_cloud=ref_cloud,
        progress="Unmorphed" if progress else False,
    )

    # warn if any JSDs negative
    for b, s in [(b, s) for b, l in _body_inv.items() for s in l]:
        neg_models = [m for m, jsds in model_jsds.items() if jsds[b][s] < 0]
        if base_jsds[b][s] < 0:
            neg_models += ["unmorphed"]
        if len(neg_models):
            logging.warning(
                f"Negative JSD for {s} under models: {' '.join(neg_models)}"
            )
    return model_jsds, base_jsds, dataset


def merge_param_hist_with_hyperparams(
    model: JointModel, params: JointModelParams, param_hist: ArrayTrace
):
    # extend hyperparameters to match the length of the param_hist
    stat, hype, _ = params.by_type()
    lengthen = lambda arr: jnp.broadcast_to(
        jnp.array(arr)[None], (len(param_hist), *jnp.array(arr).shape)
    )
    long_stat = pt.tree_map(lengthen, stat)
    long_hype = pt.tree_map(lengthen, hype)

    # form model params with batch/step dimension
    full_params = JointModelParams.from_types(
        model, long_stat, long_hype, param_hist._tree
    )
    return full_params


def select_param_step(
    model: JointModel,
    params: JointModelParams,
    param_hist: ArrayTrace,
    step: int,
):
    """Select a single step from a parameter history."""
    stat, hype, _ = params.by_type()
    return JointModelParams.from_types(
        model,
        stat,
        hype,
        pt.tree_map(lambda arr: arr[step], param_hist._tree),
    )
