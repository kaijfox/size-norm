from ._style import *
import logging
from pathlib import Path


def init_nb(
    plot_dir, style="vscode_dark", context="paper", **kws
) -> Tuple[colorset, plot_finalizer]:

    set_style(style)
    _style = colorset.active
    sns.set_context(context)
    clr = color_sets[style]
    plotter = plot_finalizer(plot_dir, **kws)
    return plotter, _style


def set_style(style):
    try:
        plt.style.use(style)
        _style = color_sets[style]
        colorset.active = _style
    except:
        try:
            plt.style.use(
                Path(__file__).parent / "stylesheets" / f"{style}.mplstyle"
            )
            _style = color_sets[style]
            colorset.active = _style
        except:
            logging.warn(
                "[init_nb] No matplotlib style `{style}` found, resorting to `default`."
            )
            style = "default"
            plt.style.use(style)
            _style = color_sets[style]
            colorset.active = _style