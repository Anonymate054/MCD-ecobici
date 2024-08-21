import fs
from fs.osfs import OSFS
from fs.subfs import SubFS

def mkdir(fs_object: OSFS, path: str) -> SubFS:
    """Ensure the directory exists; create it if it doesn't.
    
    Args:
    - fs (OSFS): An OSFS object representing the file system.
    
    Returns:
    - OSFS: The OSFS object representing the file system.
    """
    return fs_object.makedir(path) if not fs_object.exists(path) else fs_object.opendir(path)