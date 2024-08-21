# Ecobici guide installation

## Set up project's module

To move beyond notebook prototyping, all reusable code should go into the `module/` folder package. To use that package inside your project, install the project's module in editable mode, so you can edit files in the `module` folder and use the modules inside your notebooks :

```bash
pip install --editable .
```

To use the module inside your notebooks, add `%autoreload` at the top of your notebook :

```python
%load_ext autoreload
%autoreload 2
```

Example of module usage for `module`: 

```python
from module.utils.paths import data_dir
data_dir()
```
Example of module usage for default project modules.

```python
from scripts.dir_utils import dir_utils
data_dir()
```

### Considerations and Example:

Consider the stucture like: 

- mypackage/
    - __init__.py
- hello/
    - __init__.py
    - hello.py
- goodbye/
    - __init__.py
    - goodbye.py
- utils/
    - __init__.py
    - utils.py
- Notebook/
    - Test.ipynb
- setup.py

Therefore the `setup.py` file must be like this if you want an static modules:

```python
from setuptools import setup, find_packages

setup(
    ...
    packages=find_packages(include=['mypackage', 'hello', 'goodbye', 'utils']),
    ...
)
```

Or keep this stucture in order to search in all prject tree: 

```python
from setuptools import setup, find_packages

setup(
    ...
    packages=find_packages(),
    ...
)
```

And for importation modules

```python
from hello.hello import Hello
from goodbye.goodbye import Goodbye
from utils.utils import Utils

print(Hello())
print(Goodbye())
print(Utils())

```
## Set up Git diff for notebooks and lab

We use [nbdime](https://nbdime.readthedocs.io/en/stable/index.html) for diffing and merging Jupyter notebooks.

To configure it to this git project :

```bash
nbdime config-git --enable
```

To enable notebook extension :

```bash
nbdime extensions --enable --sys-prefix
```

Or, if you prefer full control, you can run the individual steps:

```bash
jupyter serverextension enable --py nbdime --sys-prefix
jupyter nbextension install --py nbdime --sys-prefix
jupyter nbextension enable --py nbdime --sys-prefix
jupyter labextension install nbdime-jupyterlab
```

You may need to rebuild the extension : `jupyter lab build`