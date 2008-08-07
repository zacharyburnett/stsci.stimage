from __future__ import absolute_import

__version__ = "3.0"

#revision based svn info
try:
    from .svn_version import __svn_version__, __full_svn_info__
except:
    __svn_version__ = 'Unable to determine SVN revision'
