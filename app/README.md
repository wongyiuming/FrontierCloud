# Python syntax reference — not a runtime

The deployable backend and updater are native Go (Gin). Python files in app/,
main.py and updater/server.py are retained only for comparing implementations
and syntax; they have no runnable-support or compatibility guarantee.

Python deployment is prohibited. The old Python Docker/Compose entrypoints
have been removed and executable entrypoints fail explicitly. Reference files
are excluded from production build context. Do not install or run them to
validate product behavior.

Maintain relevant examples alongside native changes where useful, but write
tests against Gin endpoints/native contracts. Python is supported only as a
test and script driver. Legacy serialized data formats remain supported by Go
independently of this reference collection.
