# Pióra wieczne: newsy

Raz dziennie sprawdza feedy RSS/Atom z blogów i sklepów, **zapamiętuje co już widział**
(`data/entries.json`), oznacza wpisy o premierach, nowych kolorach i edycjach limitowanych
(słowa kluczowe w `config.json`) i generuje stronę `docs/index.html` oraz własny feed `docs/feed.xml`.

## Uruchomienie (jednorazowo, ok. 10 minut)

1. Załóż repozytorium na GitHubie i wrzuć do niego całą zawartość tego folderu.
2. **Settings → Pages**: Source = "Deploy from a branch", Branch = `main`, folder = `/docs`.
3. **Actions → Update pen news → Run workflow** (pierwsze ręczne uruchomienie).
4. Po minucie strona jest pod `https://TWOJ-LOGIN.github.io/NAZWA-REPO/`. Dalej odświeża się sama.

## Źródła

Lista jest w `config.json` (`feeds`). Pole `origin` mówi skąd wziąłem adres: `feedspot` (katalog Feedspot, stan z 20.08.2026), `struktura` (wzorzec Shopify na blogu, który istnieje) albo `zgadnięty`. **Adresy feedów w paczce są domyślnymi strzałami i nie zostały
sprawdzone na żywo.** Po pierwszym uruchomieniu zajrzyj na dół strony: źródła z błędem albo puste
są tam wypisane. Żeby znaleźć poprawny adres dla nowej strony:

    python pennews.py --discover https://adres-bloga.pl

Wzorce, które zwykle działają: sklepy na Shopify `/blogs/NAZWA.atom`, WordPress `/feed/`.
Instagrama nie da się tu dopiąć (nie ma publicznego RSS), te konta trzeba obserwować w aplikacji.

## Dostosowanie

- `release_keywords`: co uznać za "premierę / edycję" (wyrażenia regularne, bez rozróżniania wielkości liter).
- `brands`: marki rozpoznawane w tytułach i opisach (filtr po marce na stronie).
- `ambiguous_brands`: nazwy zwykłych słów (Cross, Delta, Pilot…). Liczą się tylko, gdy w tekście
  jest też słowo typu "pen", "ink", "pióro".
- `retention_days`: jak długo trzymać archiwum, `new_hours`: jak długo wpis jest "nowy".

## Test lokalny

    pip install -r requirements.txt
    python pennews.py
    # otwórz docs/index.html
