.PHONY: lint test simulate css

lint:
	ruff check pikiosk tests
	mypy

test:
	pytest --cov

simulate:
	pikioskd --simulate --config config/config.example.yaml

css:
	npm run css
