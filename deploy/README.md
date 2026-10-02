Манифесты генерируются без внешних зависимостей:

```sh
python3 scripts/render.py baseline > /tmp/baseline.json
python3 scripts/render.py ft > /tmp/ft.json
minikube -p university-ft kubectl -- apply -f /tmp/baseline.json
minikube -p university-ft kubectl -- apply -f /tmp/ft.json
```

`scripts/up.sh` создаёт выделенный трёхузловой профиль, собирает образ, загружает его на узлы и ждёт готовности обоих вариантов. Имена узлов зависят от `PROFILE`, по умолчанию `university-ft`, `university-ft-m02`, `university-ft-m03`. PostgreSQL закреплён за первым узлом. FT-копии каждого приложения расположены на разных worker-узлах; baseline закреплён за m02. Резервная копия хранится на m03, отдельно от PostgreSQL. HostPath моделирует локальные диски, а не RAID.

Оба варианта имеют отдельные пространства имён, базы и тома. Kubernetes перезапускает контейнеры и в baseline; baseline отключает прикладную устойчивость и пробы, но не базовые свойства Kubernetes/PostgreSQL. Пароли и токен в генераторе предназначены только для локальной учебной среды.
