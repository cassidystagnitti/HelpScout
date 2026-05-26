import sys
import os

# Make scripts/ importable so tests can do `import app`
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
