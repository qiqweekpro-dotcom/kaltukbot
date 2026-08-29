import socket
import ssl
import time
from urllib.parse import urlparse

import requests


SITE_URL = "https://shkola-kaltuk.gosuslugi.ru"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/139.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.7,en;q=0.5",
}


def test_dns(host):
    print("\n[1] DNS")

    try:
        addresses = socket.getaddrinfo(host, 443)
        ips = sorted(set(item[4][0] for item in addresses))

        print("OK")
        print("IP:", ", ".join(ips))

        return True

    except Exception as e:
        print("ОШИБКА")
        print(type(e).__name__, e)

        return False


def test_tls(host):
    print("\n[2] TLS / SSL")

    try:
        context = ssl.create_default_context()

        with socket.create_connection((host, 443), timeout=10) as sock:
            with context.wrap_socket(
                sock,
                server_hostname=host
            ) as ssock:

                print("OK")
                print("TLS:", ssock.version())
                print("Cipher:", ssock.cipher()[0])

                return True

    except Exception as e:
        print("ОШИБКА")
        print(type(e).__name__, e)

        return False


def test_http(url, timeout=10):
    print("\n[3] HTTP")
    print(url)

    try:
        started = time.perf_counter()

        response = requests.get(
            url,
            headers=HEADERS,
            timeout=timeout,
            allow_redirects=True
        )

        elapsed = time.perf_counter() - started

        print("OK")
        print("HTTP:", response.status_code)
        print("Время:", f"{elapsed:.2f} сек.")
        print("Финальный URL:", response.url)
        print(
            "Content-Type:",
            response.headers.get(
                "content-type",
                "не указан"
            )
        )
        print(
            "Server:",
            response.headers.get(
                "server",
                "не указан"
            )
        )
        print(
            "Размер:",
            len(response.content),
            "байт"
        )

        return response

    except requests.exceptions.Timeout:
        print("TIMEOUT")
        print("Сервер не ответил вовремя.")

    except requests.exceptions.SSLError as e:
        print("SSL ERROR:")
        print(e)

    except requests.exceptions.ConnectionError as e:
        print("CONNECTION ERROR:")
        print(e)

    except requests.exceptions.RequestException as e:
        print("REQUEST ERROR:")
        print(e)

    except Exception as e:
        print("UNKNOWN ERROR:")
        print(type(e).__name__, e)

    return None


def test_robots():
    print("\n[4] ROBOTS.TXT")

    url = SITE_URL + "/robots.txt"

    response = test_http(
        url,
        timeout=8
    )

    if response:
        print("\nСодержимое robots.txt:")
        print(response.text[:5000])


def test_common_pages():
    print("\n[5] ТИПОВЫЕ СТРАНИЦЫ")

    paths = [
        "/",
        "/news/",
        "/sveden/",
        "/sveden/common/",
        "/sveden/education/",
        "/sveden/employees/",
        "/sveden/struct/",
        "/contacts/"
    ]

    for path in paths:

        url = SITE_URL + path

        print("\n>>>", path)

        try:
            started = time.perf_counter()

            response = requests.get(
                url,
                headers=HEADERS,
                timeout=(5, 8),
                allow_redirects=True
            )

            elapsed = time.perf_counter() - started

            print("HTTP:", response.status_code)
            print("Время:", f"{elapsed:.2f} сек.")
            print("Размер:", len(response.content), "байт")
            print("URL:", response.url)

        except requests.exceptions.Timeout:
            print("TIMEOUT")

        except requests.exceptions.RequestException as e:
            print(
                "ERROR:",
                type(e).__name__,
                e
            )

        time.sleep(0.5)


def main():

    print("=" * 70)

    print(
        "ДИАГНОСТИКА САЙТА "
        "ОБРАЗОВАТЕЛЬНОГО УЧРЕЖДЕНИЯ"
    )

    print("=" * 70)

    print()

    print("Сайт:")
    print(SITE_URL)

    parsed = urlparse(SITE_URL)

    host = parsed.hostname

    print()
    print("Домен:")
    print(host)

    print("=" * 70)

    dns_ok = test_dns(host)

    tls_ok = test_tls(host)

    main_response = test_http(
        SITE_URL,
        timeout=10
    )

    test_robots()

    test_common_pages()

    print()
    print("=" * 70)

    print("ИТОГ")

    print("=" * 70)

    print(
        "DNS:",
        "OK" if dns_ok else "ОШИБКА"
    )

    print(
        "TLS:",
        "OK" if tls_ok else "ОШИБКА"
    )

    if main_response:
        print(
            "HTTP:",
            f"OK ({main_response.status_code})"
        )
    else:
        print(
            "HTTP:",
            "СЕРВЕР НЕ ОТВЕТИЛ"
        )

    print("=" * 70)

    print()
    print(
        "Скопируй весь результат сюда."
    )


if __name__ == "__main__":
    main()