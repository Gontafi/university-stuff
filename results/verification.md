# Проверки — 4 октября 2026

| Проверка | Фактический результат |
|---|---|
| `go test -race -count=1 -v ./...` | PASS: retry, unsafe write, keyed write, timeout, circuit recovery, concurrent half-open, stale completion, cache isolation/expiry, fault endpoint protection |
| PostgreSQL integration test | SKIP: переменная TEST_DATABASE_URL не задана; база проекта не запущена |
| `go build ./...` | PASS |
| `go vet ./...` | PASS |
| Python unittest метрик | PASS, 3 теста: downtime/denominators, censoring, отсутствие наблюдённых отказов |
| Kubernetes manifests | JSON parsed; проверены replicas, placement, PV binding и backup node; Kubernetes API validation не выполнялась |
| Backup rehearsal config | JSON parsed; shell syntax PASS; фактический restore не запускался |
| Bash / JavaScript | Syntax PASS |
| Component experiment | Выполнен, 100 operations и 20 synthetic faults на вариант; baseline 80 successes, FT 100, FT recovered 20 |
| minikube deployment | BLOCKED: запрещён доступ к Docker socket и Kubernetes API; повышение разрешений запрещено политикой среды |
| PostgreSQL startup проекта | Не выполнен из-за блокировки deployment; существующая база другого проекта не изменялась |
| GitHub fetch / push | BLOCKED: недоступен DNS/network для github.com |

Логи фактических проверок: `go-tests.txt`, `go-build.txt`, `go-vet.txt`, `python-tests.txt`, `static-checks.txt`. Raw component dataset и расчёты: `component/`. Timestamp dataset оставлен фактическим; Git author/committer dates по запросу автора — 2 октября и ночь 3 октября 2026; timestamps измерений не изменены.

Ошибка доступа к minikube: `socket: operation not permitted`; Docker: `permission denied while trying to connect to the docker API`. Запрос повышения разрешений отклонён политикой `sandbox_approval: false`. Это ограничение исполнения, а не результат failure injection.

Из этой среды получить remote-историю не удалось. Ветка создана локально в новом репозитории со связанным origin; её история пока самостоятельна. Push без force добавляет эту новую ветку и не меняет существующие ветки GitHub. Подтверждения публикации нет.

Для завершения практической оценки в обычном терминале:

```sh
make up
make integration
make experiments
python3 scripts/metrics.py results/runs/<каталог>
git add -f results/runs/<каталог>
git commit -m "Add measured minikube experiment results"
git push -u origin midterm
```

Команды реального deployment и recovery нельзя считать проверенными лишь по синтаксической проверке. Итоговый отчёт требует дополнения полученными infrastructure measurements и сравнением с целевыми RTO/availability.
