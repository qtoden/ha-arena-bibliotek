# Bibliotek (Axiell Arena) för Home Assistant

Visar dina **boklån, reservationer och avgifter** från bibliotek som använder **Axiell Arena**
(t.ex. [bibliotek.varberg.se](https://bibliotek.varberg.se)) och låter dig **låna om** direkt från Home Assistant.

> Inofficiell integration. Arena har inget öppet API för låntagare, så integrationen loggar in
> via bibliotekets vanliga inloggningsformulär (kortnummer/personnummer + PIN) och läser *Mina sidor*.

## Installation (HACS)

1. HACS → ⋮ → **Anpassade arkiv** → lägg till detta arkiv, typ **Integration**.
2. Sök efter **Bibliotek (Axiell Arena)** och ladda ned.
3. Starta om Home Assistant.
4. **Inställningar → Enheter och tjänster → Lägg till integration → Bibliotek (Axiell Arena)**.
5. Ange bibliotekets webbadress (Varberg är förifyllt), bibliotekskortsnummer eller personnummer och **PIN-kod**.
   BankID stöds inte.

## Entiteter

| Entitet | Innehåll |
|---|---|
| `sensor.…_lan` | Antal lån. Attributet `loans` listar varje lån: titel, författare, medietyp, lånedatum, **återlämningsdatum**, om det går att låna om och antal omlån kvar |
| `sensor.…_nasta_aterlamning` | Datum för nästa återlämning, med titlar och `days_left` |
| `sensor.…_reservationer` | Antal reservationer |
| `sensor.…_reservationer_att_hamta` | Reservationer som är klara att hämta |
| `sensor.…_avgifter` | Avgifter i kr |
| `sensor.…_senast_uppdaterad` | När data senast hämtades från biblioteket (diagnostik) |
| `button.…_uppdatera` | Hämtar direkt (annars var 6:e timme) |
| `button.…_lana_om_alla` | Lånar om alla lån som går att låna om |

## Cache

Senaste hämtningen sparas i Home Assistant (`.storage`). Efter en omstart visas lånen direkt, och
är biblioteket nere fortsätter sensorerna visa senast kända data (attributet `cached: true`) och nya
försök görs var 30:e minut i stället för var 6:e timme. En nekad inloggning döljs aldrig av cachen.

## Låna om

Åtgärden `arena_bibliotek.renew`:

```yaml
action: arena_bibliotek.renew
data:
  due_within_days: 3      # bara lån som förfaller inom 3 dagar
  # titles: ["Alfons"]    # eller bara vissa titlar (delmatchning)
  # dry_run: true         # visa vad som skulle lånas om, utan att göra det
response_variable: resultat
```

Svaret innehåller per lån: `renewed`, `old_due_date`, `new_due_date` och bibliotekets meddelande.
`renewed` bygger på återlämningsdatumet när kontot läses om efteråt, inte på bibliotekets svar.

På bibliotek där knappen Låna om är avstängd tills något lån är markerat (t.ex. biblioteklb.se)
markeras lånen först ett i taget, som i en webbläsare, med en sekunds paus mellan förfrågningarna.
Då markerar också en testkörning lånen hos biblioteket och kontrollerar antalet, men skickar
inget. Markeringarna gäller bara den inloggningen.

Efter varje omlån skickas händelsen `arena_bibliotek_renewed` (antal omlånade/misslyckade + detaljer).

### Exempel: låna om automatiskt och få en notis

```yaml
triggers:
  - trigger: time
    at: "09:00:00"
conditions:
  - condition: template
    value_template: "{{ state_attr('sensor.bibliotek_varberg_nasta_aterlamning', 'days_left') | int(99) <= 2 }}"
actions:
  - action: arena_bibliotek.renew
    data:
      due_within_days: 2
    response_variable: resultat
  - action: notify.mobile_app_din_telefon
    data:
      title: Bibliotek
      message: >
        {% for kort, lan in resultat.results.items() %}{% for l in lan %}
        {{ '✅' if l.renewed else '⚠️' }} {{ l.title }} → {{ l.new_due_date }}
        {% endfor %}{% endfor %}
```

## Begränsningar

- Fungerar så länge bibliotekets Arena-sidor ser ut som idag; ändrar biblioteket sin portal kan tolkningen behöva uppdateras.
- Reservationernas layout varierar mellan bibliotek – säg till om "att hämta" inte känns igen.

---

**English:** Home Assistant integration for public libraries running Axiell Arena (common in Sweden).
Shows loans, due dates, reservations and fees, and can renew loans (`arena_bibliotek.renew`).
Logs in with library card number + PIN through the library's own web form.
