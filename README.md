# Fault-Tolerant University System

Нужны Go 1.26+, Docker, minikube, Python 3 и Bash.

Проверка проекта:

```sh
make test
make up
make integration
make demo
make experiments
make backup
make restore
```

`make up` выводит адреса baseline и FT. Результаты экспериментов — в `results/runs/`.

Без Docker и minikube: `make component`.
