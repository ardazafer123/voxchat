import os
import ssl
import sys

# The macOS build's Python looks for trusted certificates in a folder that only exists
# where it was built, so every HTTPS request would fail. macOS keeps a bundle here.
if (sys.platform == 'darwin' and 'SSL_CERT_FILE' not in os.environ
        and not os.path.exists(ssl.get_default_verify_paths().openssl_cafile)
        and os.path.exists('/etc/ssl/cert.pem')):
    os.environ['SSL_CERT_FILE'] = '/etc/ssl/cert.pem'
