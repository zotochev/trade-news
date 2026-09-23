# Админка через nginx: HTTPS и пароль

Админка trade-news слушает только `127.0.0.1:ADMIN_PORT` (по умолчанию 8080) и снаружи
недоступна. Наружу её отдаёт nginx: он принимает HTTPS, спрашивает логин и пароль
(`auth_basic`) и проксирует запросы в приложение. Сертификат бесплатный, от Let's Encrypt,
продлевается автоматически.

Ниже `admin.example.com` означает ваш домен, подставьте его. Команды для Debian.

## 0. Что нужно заранее

- Домен или поддомен, например `admin.вашдомен.ru`, у которого A-запись указывает на IP
  сервера. Проверка: `dig +short admin.вашдомен.ru` должна вернуть IP сервера.
  DNS обновляется от нескольких минут до часа.
- Открытые порты 80 и 443. Если включён файрвол `ufw`, откройте их командой
  `sudo ufw allow 'Nginx Full'`.
- Если `ADMIN_PORT` в `.env` отличается от 8080, поменяйте порт в строке `proxy_pass` конфига.

## 1. Установить nginx, certbot и htpasswd

```bash
sudo apt update
sudo apt install -y nginx certbot python3-certbot-nginx apache2-utils
```

## 2. Выпустить сертификат

```bash
sudo certbot certonly --nginx -d admin.example.com
```

При первом запуске certbot спросит email для уведомлений и попросит согласиться с условиями.
Сертификат появится в `/etc/letsencrypt/live/admin.example.com/`.

## 3. Создать логин и пароль

```bash
sudo htpasswd -c /etc/nginx/trade-news.htpasswd admin
```

Здесь `admin` — это логин, пароль команда спросит сама. Флаг `-c` создаёт файл, поэтому он
нужен только для первого пользователя. Следующих добавляйте без него:
`sudo htpasswd /etc/nginx/trade-news.htpasswd другой_логин`.

## 4. Подключить конфиг

Из папки с репозиторием (`/opt/trade-news`):

```bash
sudo cp deploy/trade-news-admin.conf /etc/nginx/sites-available/trade-news-admin
sudo sed -i 's/admin.example.com/admin.вашдомен.ru/g' /etc/nginx/sites-available/trade-news-admin
sudo ln -s /etc/nginx/sites-available/trade-news-admin /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

Команда `nginx -t` проверяет конфиг. Если она выдаёт ошибку, nginx не перезагрузится и
продолжит работать со старым конфигом.

## 5. Проверить

Откройте `https://admin.вашдомен.ru`. Браузер должен спросить логин и пароль, а после входа
показать админку. Пока самой админки ещё нет, вместо неё будет `502 Bad Gateway`: значит,
nginx и пароль уже работают, просто проксировать пока некуда.

## Продление сертификата

Сертификат действует 90 дней. Certbot сам ставит таймер и продлевает его заранее.
Проверить, что продление сработает:

```bash
sudo certbot renew --dry-run
systemctl list-timers | grep certbot
```

## Если что-то не так

| Симптом | Что проверить |
|---|---|
| certbot: `Timeout during connect` | порт 80 закрыт файрволом или DNS ещё не обновился |
| certbot: `NXDOMAIN` | у домена нет A-записи |
| `nginx -t`: `cannot load certificate` | шаг 2 не выполнен или домен в конфиге не заменён |
| `502 Bad Gateway` | trade-news не запущен или `ADMIN_PORT` не совпадает с `proxy_pass` |
| пароль не принимается | `sudo cat /etc/nginx/trade-news.htpasswd`: логин должен быть в файле |

Логи nginx: `sudo tail -f /var/log/nginx/error.log`.
