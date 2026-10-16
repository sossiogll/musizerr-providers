"""
A throwaway FTP server for scripts/integration.sh: stock pyftpdlib, passive mode, the data connection on a port of
30000-30100 that the server picks. One synthetic account; the library is /srv/ftp (inside the container, thrown away
with it). It is the stock server on purpose: it refuses SIZE in ASCII mode, as RFC 3659 allows.
"""
from pathlib import Path

from pyftpdlib.authorizers import DummyAuthorizer
from pyftpdlib.handlers import FTPHandler
from pyftpdlib.servers import FTPServer

HOME = Path("/srv/ftp")
HOME.mkdir(parents=True, exist_ok=True)

authorizer = DummyAuthorizer()
authorizer.add_user("musizerr", "synthetic-password", str(HOME), perm="elradfmwMT")
FTPHandler.authorizer = authorizer
FTPHandler.passive_ports = range(30000, 30101)
FTPServer(("0.0.0.0", 2121), FTPHandler).serve_forever()
