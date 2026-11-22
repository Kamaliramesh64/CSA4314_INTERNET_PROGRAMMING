# Paste this into /var/www/<username>_pythonanywhere_com_wsgi.py
import sys
path = '/home/Kamali/mysite'      # folder where you unzipped the project
if path not in sys.path:
    sys.path.insert(0, path)
from app import app as application
