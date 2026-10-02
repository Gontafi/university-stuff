.PHONY: test build up experiments demo backup restore component integration

test:
	go test -race ./...
	python3 -m unittest discover -s scripts -p 'test_*.py' -v

build:
	mkdir -p bin
	go build -o bin/server ./cmd/server

up:
	bash scripts/up.sh

experiments:
	python3 scripts/experiments.py

demo:
	python3 scripts/experiments.py --scenarios crash timeout transaction

backup:
	bash scripts/backup.sh backup

restore:
	bash scripts/backup.sh verify

component:
	go run ./cmd/component-experiment
	python3 scripts/metrics.py results/component

integration:
	bash scripts/integration.sh
