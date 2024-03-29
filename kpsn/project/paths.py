from ..config import (
    load_model_config,
    save_model_config,
    deepen,
    recursive_update,
)
from pathlib import Path
import os


class Project:
    def __init__(self, project_dir):
        self.project_dir = Path(project_dir)

    def root(self):
        return self.project_dir

    def scan(self, scan_name):
        return self.project_dir / "scans" / scan_name

    def model(self, model_name):
        return self.project_dir / "models" / model_name

    def main_config(self):
        return self.project_dir / "project.yml"

    def base_model_config(self):
        return self.project_dir / "base_model.yml"

    def model_config(self, model_name):
        return self.project_dir / "models" / model_name / "model.yml"

    def calibration_data(self):
        raise NotImplementedError
        return self.project_dir / "calibration.p"


def ensure_dirs(project):
    project.root().mkdir(parents=True, exist_ok=True)
    (project.root() / "scans").mkdir(exist_ok=True)
    (project.root() / "models").mkdir(exist_ok=True)


def create_model(
    project: Project, name: str = None, config=None, config_overrides={}
):
    """Create a new model directory and config file.

    Parameters
    ----------
    project : Project, or str or os.PathLike
        The project to create the model in, or if a str/PathLike then the
        path where model config should be written, alongside which model data
        will be stored.
    name : str, optional
        Name of the model. Required unless project is a str/PathLike.
    config : dict, or str/Pathlike optional
        The model config, or base model config to be overidden. If a string or
        PathLike, the path to the config file. If None, the base model config
        for the project will be used.
    """

    if isinstance(project, (str, os.PathLike)):
        model_dir = Path(project).parent
        config_path = project
        if config is None:
            raise ValueError(
                "Base config `config` is required if `project` is a string or PathLike."
            )
    else:
        if name is None:
            raise ValueError(
                "`name` is required if `project` is a string or PathLike"
            )
        model_dir = project.model(name)
        config_path = project.model_config(name)
        if config is None:
            config = project.base_model_config()

    if isinstance(config, (str, os.PathLike)):
        config = load_model_config(config)

    config = recursive_update(config, deepen(config_overrides))
    model_dir.mkdir(exist_ok=True)
    save_model_config(config_path, config)
    return model_dir, config
