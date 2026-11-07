"""Where openm454 keeps its files (state/, captures/, firmware/, the DSWave/ copy).

A source checkout (pyproject.toml next to the package) keeps them in the checkout, as before. An installed package
uses $OPENM454_HOME, else %LOCALAPPDATA%\\openm454 on Windows and ~/.local/share/openm454 elsewhere, so nothing is
written into site-packages.
"""
import os

PKG_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def home():
    env = os.environ.get('OPENM454_HOME')
    if env:
        return os.path.abspath(env)
    if os.path.exists(os.path.join(PKG_PARENT, 'pyproject.toml')):
        return PKG_PARENT
    base = os.environ.get('LOCALAPPDATA') or os.path.join(os.path.expanduser('~'), '.local', 'share')
    return os.path.join(base, 'openm454')


ROOT = home()
