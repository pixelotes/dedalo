import os

# TestClient usa "testserver" como Host; debe fijarse antes de importar dedalo.app.
os.environ.setdefault("DEDALO_ALLOWED_HOSTS", "testserver,localhost")
