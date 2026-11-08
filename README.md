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
| `sensor.…_lan` | Antal lån. Attributet `loans` listar varje lån: titel, författare, medietyp, lånedatum, **återlämningsdatum**, om det går att låna om, antal omlån kvar, post-id och omslag (`cover`) |
| `sensor.…_nasta_aterlamning` | Datum för nästa återlämning, med titlar och `days_left` |
| `sensor.…_reservationer` | Antal reservationer |
| `sensor.…_reservationer_att_hamta` | Reservationer som är klara att hämta |
| `sensor.…_avgifter` | Avgifter i kr |
| `sensor.…_senast_uppdaterad` | När data senast hämtades från biblioteket (diagnostik) |
| `button.…_uppdatera` | Hämtar direkt (annars var 6:e timme) |
| `button.…_lana_om_alla` | Lånar om alla lån som går att låna om |

## Omslag

Varje lån och reservation har attributet `cover`, en adress i Home Assistant
(`/api/arena_bibliotek/cover/<post-id>`) till bokens omslag. Biblioteket skickar omslagen utan
cache-huvuden, så integrationen hämtar varje omslag en gång, förminskar det till högst 600 × 800 px och
sparar det i Home Assistants cache-katalog (`.cache/arena_bibliotek/covers`, ingår inte i
säkerhetskopior). Sedan svarar Home Assistant själv, med lång cachetid i webbläsaren. Bara
poster som finns på ett kort hämtas. Saknar boken omslag är `cover` ändå satt men svarar 404,
och biblioteket tillfrågas igen först efter en vecka. Omslag till böcker som inte längre finns
på något kort tas bort efter 30 dagar.

Har biblioteket lagt till eller bytt ett omslag tömmer åtgärden `arena_bibliotek.clear_covers`
cachen, och varje omslag hämtas igen nästa gång det visas. Cachen töms också av sig själv vid
start om den största storleken har ändrats i en ny version.

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
