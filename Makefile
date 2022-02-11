all: install

i ins install:
	@echo "installing dependencies and rebuild"
	pip install .
	pip install -r requirements.txt