# 🎯 Memecoin Radar

Un bot qui surveille les posts (X, Truth Social via RSS, news) et le flux
temps réel de **pump.fun**. Quand un post lance un narratif, il repère les
tokens qui le reprennent, fait des **contrôles anti-rug** et t'envoie une
**alerte Telegram** détaillée. Ensuite il continue de surveiller le token et te
prévient si le dev vend.

Il peut aussi **trader tout seul** sur les meilleures alertes, avec une gestion du
risque stricte. Par défaut il trade en **mode papier** (argent virtuel). Le mode
réel est désactivé tant que tu ne l'actives pas toi-même.

> Le bot n'a **jamais** besoin de ta seed phrase ni de tes mots de passe. En mode
> réel seulement, il utilise la clé privée d'un **wallet dédié** que tu crées
> exprès, stockée chez toi dans `.env` : les transactions sont signées sur ta
> machine, la clé n'est envoyée nulle part.

---

## ⚠️ À lire avant de mettre 1 €

- **La grande majorité des tokens pump.fun meurent.** D'après CoinGecko, ~69 % des
  tokens font leur dernier trade le jour du lancement et ~4,5 % passent 90 jours.
  Solidus Labs classait ~98,6 % des tokens comme rug pulls ou pump & dumps (une
  mesure basée sur l'illiquidité, pas des fraudes prouvées).
- **Tu ne battras pas les bots sur la vitesse.** Quand Trump poste, des bots
  « snipers » créent et achètent les tokens en moins d'une seconde. Le temps que
  l'alerte arrive et que tu cliques, ils sont déjà là, et souvent ils te vendent
  leurs tokens. Ce radar sert à **mieux choisir et à éviter les arnaques**, pas à
  être le premier.
- **Commence en « paper trading ».** Laisse tourner le bot 1 ou 2 semaines sans
  acheter, puis tape `/bilan` : il calcule ce que tu aurais gagné ou perdu en
  achetant à chaque alerte. Si le bilan est négatif (c'est le plus probable),
  tu as économisé ton argent.
- **Sécurité :** utilise un wallet dédié avec un petit montant. Ne donne ta seed
  phrase ou ta clé privée à personne : ni à un humain, ni à un bot, ni à une IA.
  Tout ce dont le projet a besoin reste chez toi, dans le fichier `.env`.

---

## Ce que fait le bot

```
 Posts X / Truth Social / news ──► mots-clés (+ analyse IA optionnelle)
                                          │  narratif actif 45 min
 Flux pump.fun (PumpPortal) ──► nouveau token ──► correspond au narratif ?
                                                         │ oui
                       observe les trades 20 s ◄─────────┘
                                │
     contrôles anti-rug : achat du dev, créateur en série, top holders,
     snipers/bundle, RugCheck, réseaux sociaux, dev qui vend...
                                │
            alerte Telegram ──► suivi 30 min : « le dev vend ! », « -50 % »
                                │
                     bilan paper trading (/bilan)
```

- **Détection des narratifs** : tickers `$XXX`, hashtags, mots en MAJUSCULES,
  noms propres, phrases entre guillemets... Les mots trop courants sur pump.fun
  (« trump », « pepe », « cat »...) comptent moins, automatiquement.
- **Analyse IA (optionnelle)** : Claude lit le post, estime s'il peut lancer une
  vague de memecoins (score de hype), devine les noms/tickers probables et donne
  un avis sur chaque alerte.
- **Tokens créés juste avant le post** : le bot vérifie aussi les 10 dernières
  minutes (les bots sont parfois plus rapides que l'API X).
- **Copies** : sur un gros narratif il y a des dizaines de tokens identiques ;
  l'alerte indique combien de copies existent et le rang du token en volume.
- **`/check <adresse>`** : analyse anti-rug de n'importe quel token, à la demande.

---

## 📺 Lire les tweets gratuitement (chaînes Telegram)

L'API X est payante. À la place, le bot peut lire des **chaînes Telegram publiques
qui republient les tweets** (Elon Musk, Trump...) en quelques secondes, avec ton
compte Telegram. C'est gratuit.

1. Va sur **https://my.telegram.org**, connecte-toi, ouvre **« API development
   tools »** et crée une application (nom et description au choix). Copie
   **api_id** et **api_hash** dans `.env` (`TG_API_ID=` et `TG_API_HASH=`).
2. Une seule fois, dans le dossier du bot : `python -m memebot telegram-login`.
   Entre ton numéro, puis le code reçu dans Telegram.
3. Relance le bot, puis envoie-lui `/chaine @nom_de_la_chaine` pour chaque chaîne
   à suivre. Le bot rejoint la chaîne et analyse chaque nouveau message.
   `/chaines` liste les chaînes, `/retirer @nom` en enlève une.

- Choisis des chaînes qui postent **vite** et **seulement** les tweets (pas de pubs).
- Les messages de ces chaînes ne sont **pas annoncés un par un** : tu reçois une
  alerte seulement quand un token correspond.
- ⚠️ Le fichier `telegram.session` et les clés `TG_API_*` donnent accès à ton
  compte Telegram : ne les partage jamais. Le bot ignore tes conversations privées.
- Sur le serveur : même chose, `cd ~/memecoin-radar && .venv/bin/python -m memebot
  telegram-login`, puis `sudo systemctl restart memebot`.

---

## 🤖 Trading automatique

Quand une alerte passe des filtres **plus stricts** (sécurité ≥ 60, momentum ≥ 30,
market cap < 100 SOL, token de moins de 3 min), le bot achète, puis gère seul la
sortie :

| Règle | Réglage par défaut |
|---|---|
| Taille de position | 35 % du capital disponible, une seule position à la fois |
| Réserve intouchable | 0,008 SOL pour les frais et le loyer du compte token |
| Stop-loss | -25 % |
| Prise de profit | vend 50 % à x2 (la mise est récupérée) |
| Trailing stop | dès +40 %, vend tout si le prix recule de 30 % depuis son plus haut |
| Le dev vend | vente immédiate |
| Ça ne décolle pas | vente si moins de +10 % après 15 min, ou si plus aucun échange depuis 5 min |
| Perte du jour | plus d'achat si -35 % depuis minuit (UTC) |
| Pertes d'affilée | pause de 2 h après 3 pertes |
| Coupe-circuit | arrêt complet à -60 % depuis le plus haut (`/trading on` pour relancer) |
| Wallet trop rempli | refuse de trader si le wallet contient plus de 0,5 SOL |

### Ce que coûte chaque trade (impossible à éviter)

- **Frais par ordre** : environ 1,25 % pour pump.fun et 0,5 % pour PumpPortal, soit
  ~3,5 % pour un aller-retour.
- **Frais réseau et priorité** : environ 0,0006 SOL par aller-retour. Sur une
  position de 0,015 SOL (~2 $), ça fait encore ~4 %.
- **Loyer du compte token** : ~0,002 SOL par nouveau token. Le bot le récupère en
  fermant le compte après la vente ; si la fermeture échoue, il est perdu.

Avec 7 $, chaque aller-retour coûte donc **~7-8 %** avant même de gagner quoi que
ce soit. Le mode papier compte ces frais.

### Passer en mode réel (seulement après des semaines de papier positif)

1. Crée un **nouveau wallet** dans Phantom (« Ajouter un compte »), uniquement pour le bot.
2. Envoie-lui le petit montant que tu acceptes de perdre (pas plus de 0,5 SOL).
3. Exporte sa clé privée (Phantom → Paramètres → le compte → « Afficher la clé
   privée ») et colle-la dans `.env` à `WALLET_PRIVATE_KEY`. **Nulle part ailleurs.**
4. Dans `config.toml`, section `[trading]`, mets `mode = "live"`, puis relance le bot.

En mode réel, PumpPortal construit la transaction et le bot la signe sur ta
machine. Le bot vérifie que la transaction est bien pour ton wallet, mais tu fais
quand même confiance à PumpPortal : c'est pour ça qu'il ne faut laisser que peu
d'argent sur ce wallet.

### Tout gratuit ?

Le bot lui-même ne coûte rien. Telegram, le flux pump.fun de PumpPortal,
Truth Social par RSS, les news RSS et le RPC Helius (offre gratuite) sont
gratuits. Les seules dépenses sont les frais de trading ci-dessus, payés à
pump.fun, à PumpPortal et au réseau Solana. Deux options sont payantes, à toi de
voir : l'API X (sans elle, on suit Trump via Truth Social) et l'IA (sans elle, le
bot utilise l'extraction de mots-clés).

---

## Installation

Il faut Python 3.11 ou plus récent (https://www.python.org/downloads/).
**Toutes les commandes se tapent dans le dossier du bot** (celui qui contient
`README.md` et le dossier `memebot`), sinon Python répond « No module named memebot ».

### Windows

1. Télécharge le bot : bouton vert **Code → Download ZIP** sur GitHub, puis clic droit
   sur le ZIP → **Extraire tout**.
2. Ouvre le dossier extrait jusqu'à voir `README.md` et `memebot`. Clique dans la barre
   d'adresse de l'explorateur, tape `cmd`, Entrée : un terminal s'ouvre **dans ce dossier**.
3. Tape, une ligne à la fois :

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy config.example.toml config.toml
copy .env.example .env
python -m memebot --demo
```

Ensuite, tout se fait en double-clic dans le dossier du bot :

| Fichier | Effet |
|---|---|
| `lancer-bot.bat` | Lance le bot (installe ou répare tout seul ce qui manque) |
| `mettre-a-jour.bat` | Installe la dernière version depuis GitHub, **sans toucher** à `.env`, `config.toml`, la base `memebot.db` ni la session Telegram (ferme le bot avant) |
| `connexion-telegram.bat` | Connexion unique à ton compte Telegram pour lire des chaînes |

Pour qu'il démarre tout seul avec Windows : `Win + R`, tape `shell:startup`, Entrée,
puis fais clic droit → Nouveau → Raccourci vers `lancer-bot.bat`.

Évite de mettre le dossier dans **OneDrive** (Bureau ou Documents synchronisés) :
la synchronisation peut gêner la base de données. `C:\memecoin-radar` est un bon endroit.

### Mac / Linux

```bash
git clone https://github.com/Orionms810/Orionms810-memecoin-radar.git
cd Orionms810-memecoin-radar
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config.example.toml config.toml
cp .env.example .env
python -m memebot --demo
```

La démo ne demande aucune clé ni connexion.

### Serveur gratuit 24 h/24 (Google Cloud)

1. Crée une VM **e2-micro** (offre gratuite) : région **us-central1**, **us-east1** ou
   **us-west1**, système **Debian 12**, disque **« Standard persistent disk »** de 30 Go
   maximum, sans adresse IP statique.
2. Clique sur **SSH** à côté de la VM : un terminal s'ouvre dans le navigateur.
3. Colle cette commande :

```bash
curl -fsSL https://raw.githubusercontent.com/Orionms810/Orionms810-memecoin-radar/claude/memecoin-trading-bot-pumpfun-jbsdzq/deploy/install-serveur.sh | bash
```

4. Remplis `.env` (`nano ~/memecoin-radar/.env`), puis `sudo systemctl restart memebot`.

Commandes utiles sur le serveur :

| Commande | Effet |
|---|---|
| `journalctl -u memebot -f` | Logs en direct (Ctrl + C pour quitter) |
| `sudo systemctl restart memebot` | Redémarre le bot |
| `sudo systemctl stop memebot` | Arrête le bot |
| `bash ~/memecoin-radar/deploy/mettre-a-jour.sh` | Installe la dernière version |

⚠️ Ne fais pas tourner le bot à deux endroits en même temps avec le même token Telegram.

## Les clés à mettre dans `.env`

| Clé | Obligatoire ? | Où l'obtenir | Prix |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Oui (sinon alertes dans le terminal) | Parle à **@BotFather** sur Telegram → `/newbot` | Gratuit |
| `TELEGRAM_CHAT_ID` | Oui | Lance le bot, envoie `/start` à ton bot : il te répond ton chat id | Gratuit |
| `SOLANA_RPC_URL` | Conseillé | Compte gratuit chez Helius, QuickNode... | Gratuit (palier de base) |
| `X_BEARER_TOKEN` | Pour suivre X | developer.x.com → ton app → « Bearer Token » | Payant à l'usage (vérifie la grille dans ta console X) |
| `ANTHROPIC_API_KEY` | Non (IA) | console.anthropic.com | Payant à l'usage, quelques centimes par jour avec peu de posts |
| `WALLET_PRIVATE_KEY` | Seulement en mode réel | Wallet **dédié** créé exprès (voir plus haut) | — |

Pour Trump, pas besoin de l'API X : il poste surtout sur **Truth Social**, suivi
via un flux RSS gratuit (voir `config.toml`, vérifie que l'URL fonctionne).

## Lancer le bot

```bash
python -m memebot                 # mode réel (Ctrl+C pour arrêter)
python -m memebot check <adresse> # analyse un token dans le terminal
python -m memebot -v              # logs détaillés
```

Pour qu'il tourne 24 h/24, lance-le sur un petit serveur (VPS) ou un Raspberry Pi
avec `tmux`, `screen` ou un service `systemd`.

## Commandes Telegram

| Commande | Effet |
|---|---|
| `/check <adresse>` | Rapport anti-rug d'un token |
| `/narratif covfefe, $COVF` | Surveille un narratif à la main (une news vue à la télé...) |
| `/narratifs` | Liste les narratifs actifs |
| `/supprimer N3` | Arrête un narratif |
| `/bilan` | Bilan paper trading des alertes |
| `/analyse` | Ce que sont devenus **tous** les tokens repérés (alertés et refusés) 30 min après |
| `/export` | Fichier CSV (Excel) de tous les candidats et de leurs mesures |
| `/status` | État du bot |
| `/pause` · `/reprendre` | Coupe / remet les alertes |
| `/trading` · `/trading on` · `/trading off` | État du trading, l'activer, le couper |
| `/positions` | Positions ouvertes |
| `/vendre tout` · `/vendre $TICKER` | Vente immédiate (bouton panique) |
| `/journal` | Derniers trades et résultats |
| `/chaine @nom [poids]` | Suivre une chaîne Telegram (tweets republiés) |
| `/chaines` · `/retirer @nom` | Chaînes suivies / en retirer une |

## Lire une alerte

```
🎯 TOKEN LIÉ À UN NARRATIF — 🟡 RISQUÉ (sécurité 62/100)
Nugget Nation ($NN)
📰 Narratif N1 : @realDonaldTrump — « ... NUGGET ... #NuggetNation »
🔑 Mots-clés : nuggetnation, nugget (match 1.00)
💰 Market cap : 38.8 SOL (~$5.8k) · courbe 5%
⏱ Âge : 20 s · 2 autre(s) token(s) sur ce narratif, celui-ci #2 en volume
📈 Flux : 9 achats / 0 ventes · 9 acheteurs uniques
⚡ Momentum : 58/100
🛡 Contrôles anti-rug
⛔ 9 wallets ont acheté dans les 2 premières secondes (bundle/snipers probable)
⚠️ Top 10 holders : 34% de la supply
✅ Achat initial du dev faible : 4.0%
```

- **Sécurité** (0-100) : 🟢 ≥ 70, 🟡 40-69, 🔴 < 40 ou signal critique (le dev a
  vendu). 🟢 veut dire « pas de signal d'arnaque évident », **pas** « sûr ».
- **Momentum** : est-ce que de vrais acheteurs arrivent ?
- **Match** : à quel point le nom colle au narratif.
- Les tokens 🔴 ne sont pas envoyés par défaut (`show_dangerous = true` dans `[alerts]` pour les voir).

## Réglages utiles (`config.toml`)

- Trop d'alertes → monte `min_match_score` (0.7) et `min_safety_to_alert` (60).
- Plus de vitesse → baisse `observe_seconds` (moins de données sur le token).
- Ajoute/retire des comptes X, des flux RSS, des mots génériques.

## Limites connues

- PumpPortal et l'API pump.fun sont **non officielles** : leur format peut changer.
- La réactivité dépend de la source : X est interrogé toutes les 20 s, le RSS
  Truth Social ajoute son propre délai.
- Le matching par mots-clés rate les tokens nommés uniquement en emoji ou dans
  une autre langue, et peut matcher des homonymes.
- La détection des snipers est approximative (heure de réception, pas le bloc).
- Après la migration vers un AMM, le pool peut apparaître comme gros holder.
- Aucun outil ne peut garantir qu'un token n'est pas une arnaque.
- Le mode réel n'a été testé qu'avec des réponses simulées de PumpPortal et du RPC
  Solana : vérifie les premières transactions sur Solscan avec de tout petits montants.
- Un stop-loss ne protège pas d'un rug instantané : si le dev vend tout d'un coup,
  le prix peut passer de +0 % à -90 % entre deux trades.

## Structure du code

```
memebot/
  engine.py      cerveau : relie news, flux pump.fun, analyse, alertes
  text.py        extraction des mots-clés d'un post
  narratives.py  narratifs actifs + matching avec les tokens
  risk.py        score de sécurité anti-rug + momentum
  data.py        RPC Solana, RugCheck, DexScreener, IPFS, prix du SOL
  pumpportal.py  websocket temps réel pump.fun
  sources/       X (API v2) et RSS
  ai.py          analyse des posts et avis sur les alertes (Claude)
  trading/       trading auto : gestion du risque, exécution papier et réelle
  telegram.py    envoi des alertes et commandes
  report.py      mise en forme des messages
  storage.py     SQLite : tokens, alertes, bilan paper trading
  demo.py        démo hors-ligne
tests/           tests (pytest), sans accès réseau
```

```bash
pip install -r requirements-dev.txt
python -m pytest
```
