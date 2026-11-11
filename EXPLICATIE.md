# Explicația proiectului (rezumat în română)

Un rezumat ca să înțelegi proiectul și să-l poți explica. Detaliile tehnice complete sunt
în `README.md`, iar în cod comentariile în română încep cu `[RO]`.

## 1. Ce este, într-o frază

O aplicație web pentru filme (CRUD), rulată pe **mai multe servere**, fiecare cu **propria
bază MongoDB**. În fața lor stă un **smart proxy** care împarte cererile cu algoritmul
**Power of Two Choices**, ține un **cache** și ocolește serverele căzute. Serverele își
**sincronizează singure datele** între ele.

## 2. Cele 4 componente

```
browser ──▶ PROXY :8080 ──(P2C)──▶ node1 :8001 ── MongoDB :27101
               │                   node2 :8002 ── MongoDB :27102
               │                   node3 :8003 ── MongoDB :27103
               │                      ▲  ▲  copiază schimbările unul de la altul la 2 s
               └── REGISTRAR :9100 ◀──┘  heartbeat la 3 s
```

| Componentă | Fișier | Rol, pe înțeles |
|---|---|---|
| **Proxy** | `proxy/main.py` | „Recepția”: primește toate cererile, alege serverul, ține cache-ul, blochează abuzurile |
| **Registrar** | `registrar/main.py` | „Cartea de telefon”: știe ce servere sunt vii în acest moment |
| **Server (node)** | `dw_node/main.py` | API-ul pentru filme |
| — baza de date | `dw_node/store.py` | Scrierile în MongoDB, în tranzacții, și regula de conflict |
| — sincronizare | `dw_node/sync.py` | Copierea schimbărilor de la celelalte servere |
| **Launcher** | `run.py` | „Telecomanda”: pornește și oprește procesele. Nu face parte din sistem |

## 3. Drumul unei cereri (exemplu: adaugi un film)

1. Browserul trimite `POST /movies` la proxy (`:8080`). Clientul nu cunoaște alte adrese.
2. **Filtrare** (`filter_request`): metodă permisă? Prea multe cereri de la tine? Cale internă?
3. **Alegerea serverului** (`pick_p2c`): se trag la sorți 2 servere sănătoase și se alege
   cel cu mai puține cereri în lucru.
4. **Serverul salvează** (`local_write`): într-o singură tranzacție scrie filmul (`version: 1`,
   `node: node2`) și o intrare în jurnalul de schimbări (`changes`, cu numărul 18, de exemplu).
5. Proxy-ul **golește cache-ul** și îți răspunde cu `201`. Antetul `X-Node` îți spune ce server a lucrat.
6. În maximum ~2 s, node1 și node3 întreabă node2 „ce ai nou după nr. 17?” și copiază filmul.

## 4. Conceptele importante și cum să le explici

### Power of Two Choices (P2C)
> „Pentru fiecare cerere aleg la întâmplare **două** servere și o trimit la cel **mai puțin
> ocupat**. Nu trebuie să verific toate serverele, reacționez la un server lent (acela strânge
> cereri în lucru, deci pierde comparația), și evit «efectul de turmă»: dacă aș alege mereu
> cel mai liber server, toate cererile simultane s-ar duce pe același server.”

Codul are 4 rânduri: `pick_p2c` în `proxy/main.py`. HAProxy are același algoritm: `balance random(2)`.

### Health check și failover
> „La fiecare 2 s proxy-ul cere `/health` fiecărui server. Serverul răspunde OK doar dacă și baza
> lui merge. După 2 eșecuri la rând îl scot din rotație, iar după 2 reușite îl pun înapoi.”

Regula de failover, adică **când e voie să trimiți cererea la alt server**:
- conexiune refuzată → cererea n-a ajuns → trimit la altul, **chiar și un POST**;
- serverul zice „baza mea e jos, n-am salvat” (`503` + `X-Retry-Safe`) → trimit la altul;
- serverul nu răspunde în 15 s → **NU** reîncerc, pentru că poate a salvat deja și aș crea filmul de două ori → `504`.

### Cache
- Răspunsurile `GET /movies` se păstrează **5 s** (`HIT`).
- După orice scriere, cache-ul se golește și, timp de **5 s**, citirile merg la serverul care a
  făcut scrierea și **nu se pun în cache**. Altfel citirea putea ajunge la un server care încă
  n-a copiat schimbarea, iar lista veche ar fi stat în cache 5 s („read-your-writes”).
- Dacă **toate** serverele sunt căzute, se servește o copie veche de până la **60 s** (`STALE`)
  în loc de eroare. Scrierile primesc `503`.
- Când expiră o intrare, doar o cerere merge la server, iar celelalte așteaptă (protecție la „cache stampede”).

### Sincronizarea bazelor de date (partea cea mai grea)
Fiecare film are **`version`** (crește la fiecare modificare) și **`node`** (cine a făcut-o).

1. **Scriere locală în tranzacție:** filmul și intrarea în jurnal se salvează împreună sau deloc.
2. **Pull la 2 s:** fiecare server cere de la ceilalți „schimbările după nr. N”. Ține minte N
   în propria bază, deci după un restart continuă de unde a rămas. Fiecare peer are **bucla lui
   separată**, așa că un server căzut sau lent nu întârzie sincronizarea dintre celelalte.
3. **Regula de conflict, aceeași pe toate serverele:** câștigă perechea mai mare `(version, node)`.
   Ceasul nu se folosește, pentru că ceasurile serverelor pot diferi.
4. **Ștergerea e „tombstone”** (`deleted: true`), ca să se sincronizeze ca orice altă schimbare
   și ca un update mai vechi să nu „învie” filmul.
5. **Reconciliere completă la 60 s** și la revenirea unui server: se compară lista
   `(id, version, node)` și se copiază ce lipsește. Asta e plasa de siguranță.

Dovada: `http://127.0.0.1:8001/sync/checksum` (la fel pe 8002 și 8003). Același hash înseamnă date identice.

### Conflicte între utilizatori (If-Match)
> „Când editezi, clientul trimite «modifică doar dacă e încă versiunea 3». Dacă altcineva a
> salvat între timp, primești `409 Conflict`, în loc să-i suprascrii modificarea în tăcere.”

### Service discovery
> „Niciun server nu e scris în cod. Fiecare server anunță registrarul la 3 s. Dacă tace 10 s,
> e scos din listă. Un server nou (`run.py add-node 4`) apare singur în proxy.”

## 5. Ce se întâmplă la căderi

| Cade... | Ce se vede | De ce merge în continuare |
|---|---|---|
| un server | roșu în dashboard în ~4 s | proxy-ul trimite cererile la celelalte servere |
| o bază de date | baza roșie, serverul galben | `/health` dă 503, deci serverul iese din rotație |
| serverul revine | galben → verde | `heartbeat` → reconciliere → pull: recuperează tot ce a pierdut |
| toate serverele | lista vine din cache (`STALE`) | copia de rezervă de 60 s; scrierile dau 503 |

## 6. Compromisuri (bine de spus singur, înainte să te întrebe)

- **Consistență eventuală, nu imediată:** o citire pe alt server poate fi cu ~2 s în urmă. Este
  prețul pentru ca fiecare server să poată scrie singur, fără să aștepte după ceilalți (teorema CAP:
  am ales disponibilitatea).
- **Editări simultane pe servere diferite:** se păstrează una singură, aceeași pe toate serverele,
  iar cealaltă se pierde. If-Match protejează doar editările care ajung pe același server.
- **Proxy-ul e un singur punct de cădere** (o singură instanță). Registrarul e la fel, dar doar
  pentru înregistrări noi.
- Fără autentificare și TLS: sunt în afara temei laboratorului.

## 7. Întrebări probabile și răspunsuri scurte

| Întrebare | Răspuns |
|---|---|
| De ce MongoDB e „replica set” dacă fiecare server are baza lui? | Tranzacțiile din MongoDB merg doar în mod replica set. E un set cu **un singur membru**, deci nu există replicare între servere. Sincronizarea e scrisă de noi. |
| De ce pull și nu push? | Un server căzut nu blochează pe nimeni. Când revine, își cere singur ce a pierdut, pornind de la ultimul număr salvat. |
| De ce nu se folosește ora la conflicte? | Ceasurile serverelor diferă. `(version, node)` dă același rezultat pe toate serverele, indiferent de oră. |
| Ce se întâmplă dacă aceeași schimbare ajunge de 2 ori? | Nimic, pentru că aplicarea e idempotentă: se scrie doar dacă e mai nouă decât ce există. |
| De ce serverele nu-și retrimit schimbările primite de la alții? | Fiecare copiază direct de la fiecare. Retransmiterea ar crea ecouri la nesfârșit. |
| De ce P2C și nu round robin? | Round robin ignoră cât de ocupat e un server. P2C ține cont de asta și costă aproape la fel de puțin. |
| Cum demonstrezi că bazele sunt identice? | `/sync/checksum` pe fiecare server: același hash. `tests/demo.py` verifică asta automat. |

## 8. Demonstrația în 3 minute

1. `python run.py start`, apoi deschizi clientul (`:8080`) și dashboard-ul (`:8080/_proxy`) alături.
2. Dai **Reload** de câteva ori: `server:` se schimbă (P2C), iar `cache: HIT` apare la cererile repetate.
3. Adaugi un film și arăți `/sync/checksum` pe 8001, 8002 și 8003: hash identic.
4. **Stop server** la node2, adaugi un film: merge. **Start server**: devine verde, iar filmul apare și pe node2.
5. **Stop database** la node3: serverul devine galben, aplicația merge. **Start**: totul revine la verde.
6. Rulezi `python tests/demo.py`: toate funcționalitățile verificate automat.
