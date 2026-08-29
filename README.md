# agent-runner-langgraph

Le demon de revue, pilote par un graphe [LangGraph](https://langchain-ai.github.io/langgraph/).

Portage de `claude-agent-runner`, **qui n'est pas modifie**. Les deux depots
coexistent ; celui-ci a sa propre base d'etat, ses propres worktrees et son
propre port (cf. l'en-tete de `runner.yaml`).

## Installation

### Ce qu'il faut avant

- **Python 3.11+** et **git**.
- Un jeton de la forge en **lecture** (`repo:read`). Le jeton d'**ecriture** peut
  attendre : sans lui, l'agent corrige dans son worktree sans rien rendre
  visible, et c'est un bon premier cran.
- Un **jeton OAuth Claude** (`CLAUDE_CODE_OAUTH_TOKEN`) : la consommation
  s'impute sur l'abonnement, pas sur l'API payante.
- Les toolchains de vos depots (Node, venv…) : le demon lance leurs
  verifications, il ne commite pas de code dont les tests echouent.

### En local

```bash
python -m venv .venv
.venv/bin/pip install -e ".[keyring]"      # Windows : .venv\Scripts\pip.exe
.venv/bin/agent-runner-lg init
```

`init` pose les questions, **verifie chaque jeton contre la forge**, liste vos
depots, devine les verifications en lisant `package.json` et `pyproject.toml`,
puis ecrit `runner.yaml` et `profils/<projet>.yaml`.

Rien n'est ecrit avant la derniere question, et un fichier existant est
sauvegarde a cote avant d'etre remplace.

### En conteneur

```bash
cp .env.exemple .env          # y mettre les jetons
UID=$(id -u) GID=$(id -g) docker compose build
```

**Le conteneur a ses PROPRES clones.** On les depose une fois dans son volume :

```bash
docker compose run --rm outils -lc \
  "git clone https://x-access-token:$PAT_READ@github.com/ORG/DEPOT.git /repos/DEPOT
   cd /repos/DEPOT && npm ci"
```

Puis :

```bash
docker compose up -d
```

Console sur <http://127.0.0.1:8788>.

#### Pourquoi le conteneur ne monte pas vos dossiers

C'est la question naturelle, et elle a une reponse mesuree. Un `npm ci` lance
dans un conteneur sur un depot **monte depuis l'hote** a remplace son
`node_modules` Windows par des binaires Linux — plus un seul `.cmd` dans
`.bin/`, que des liens symboliques. L'environnement de developpement de l'hote
etait casse, et rien ne l'annoncait : la commande s'etait terminee par
« added 619 packages ».

Le probleme est structurel, pas accidentel :

- le conteneur est Linux, l'hote souvent non ; `node_modules` et `.venv` ne se
  partagent pas entre les deux ;
- le demon a pourtant besoin des dependances **a cote du depot** pour lancer les
  verifications — il ne commite pas de code dont les tests echouent.

Deux besoins incompatibles sur les memes fichiers. La seule sortie propre est de
ne pas les partager : le conteneur clone ce dont il a besoin, chez lui. C'est ce
que fait n'importe quel agent de CI.

Sur un hote **Linux**, et seulement la, un bind mount reste possible et economise
le disque — remplacer `- repos:/repos` par `- ${REPOS}:/repos` dans le compose.

#### Trois autres differences

| | |
|---|---|
| **Secrets** | pas de trousseau : les references doivent etre `env:NOM`, alimentees par le `.env` |
| **Chemins** | le profil reste ECRIT POUR L'HOTE ; `AGENT_RUNNER_WORKSPACE=/repos`, pose par le compose, reecrit `workspace` au chargement |
| **`runner.yaml`** | celui du conteneur n'est pas celui d'un poste : etat sous `/var/agent-runner`, `bind: 0.0.0.0` avec `reseau_confine: true`. `exemples/runner.yaml` est ecrit pour ca |

## Configuration

Deux fichiers, et la separation compte : **la machine** d'un cote, **les
projets** de l'autre. Copier un profil suffit a ajouter un projet ; le moteur
n'est pas touche.

| Fichier | Contenu | Copie ? |
|---|---|---|
| `runner.yaml` | chemins, API locale, cadence, moteur, armement | jamais |
| `profils/<projet>.yaml` | forge, depots, verifications, relecteurs, budget | un par projet |

### Cinq regles qui portent le reste

1. **Aucun secret dans le YAML.** Uniquement des references, resolues au moment
   de l'usage : `env:PAT_WRITE` ou `keyring:agent-runner-lg/PAT_WRITE`. Une
   valeur en clair est **refusee** a la validation.
2. **Cle inconnue = erreur.** Un `acces:` mal orthographie refuse de demarrer au
   lieu de retomber sur un defaut permissif.
3. **Les defauts sont les valeurs sures.** `default_access: context`,
   `writes_enabled: false`. Une cle absente donne toujours l'agent le plus
   inoffensif.
4. **Deux crans d'armement.** `writes_enabled` autorise l'agent a tourner ;
   `forge.token_write` autorise le push et **toute** ecriture sur la forge.
   Sans le second, l'agent corrige et personne n'est prevenu de rien.
5. **`check` distingue « valide » de « operant ».** Une configuration correcte
   peut ne rien pouvoir faire. `check` liste ce qui manque pour AGIR.

### Les secrets

Le stockage « chiffre sur disque » est une fausse bonne idee, et le demon ne le
propose pas : un demon qui redemarre seul doit dechiffrer seul, donc la cle est
a sa portee, sur la meme machine, souvent dans le meme dossier. Une obfuscation
prise pour du chiffrement est pire que rien, parce qu'on cesse de se mefier.

| Forme | Ou vit la cle | Quand l'employer |
|---|---|---|
| `keyring:SERVICE/COMPTE` | **le systeme** (Credential Manager, Keychain, libsecret) | poste de travail |
| `env:NOM` | l'environnement du processus | conteneur, service, CI |

`init` choisit le trousseau quand il en trouve un, et retombe sur `env:` sinon —
en affichant les variables a poser.

### Le moteur

Configurable globalement et **par severite de remarque** : un defaut de
correction merite qu'on y mette les moyens, une coquille de nommage non.

```yaml
model: claude-sonnet-5
per_severity:
  P1: { model: claude-opus-5, effort: high }
  P3: { model: claude-sonnet-5, effort: low }
```

`--model` et `--effort` surchargent pour un lancement, table par severite
comprise — sinon on croirait tourner sur le modele demande alors que non.

### Les commandes

```bash
agent-runner-lg -c runner.yaml init      # l'assistant
agent-runner-lg -c runner.yaml check     # valide, et dit ce qui manque pour AGIR
agent-runner-lg -c runner.yaml status    # ce que le demon ferait, sans rien faire
agent-runner-lg -c runner.yaml run       # un passage
agent-runner-lg -c runner.yaml serve     # le demon + la console
```

`status` avant d'armer, toujours : on observe d'abord.

### Comment il detecte les revues

Il ne sonde pas en continu. **Une requete GraphQL par depot, toutes les cinq
minutes** (`wake.reconcile_every`), qui ramene d'un coup les PR ouvertes, leurs
checks et leurs fils de revue. Trois depots font 36 requetes a l'heure, contre
5 000 points autorises.

Le modele est **declenche sur niveau** : on ne reagit pas a un evenement, on
relit l'etat et on en deduit le travail. Un reveil manque ne coute donc que de
la latence, jamais du travail — et une livraison webhook perdue se rattrape
toute seule au passage suivant.

## Plusieurs developpeurs

Le modele retenu : **un demon par developpeur**. Chacun lance le sien, avec ses
propres jetons, sur ses propres PR.

### Pourquoi le perimetre n'est pas optionnel

Les baux — ce qui empeche deux jobs de prendre la meme PR — vivent dans une base
**sqlite locale**. Deux demons sur deux machines ont deux bases, donc **aucune
exclusion mutuelle**. Sans perimetre, ils prendraient la meme PR au meme moment,
pousseraient sur la meme branche, repondraient deux fois dans les memes fils, et
consommeraient deux fois le quota.

Le bail ne peut pas resoudre ca sans stockage partage. Ce qui le resout, c'est de
rendre les ensembles de travail **disjoints** :

```yaml
scope:
  authors:
    - moi          # ne traiter que les PR que J'AI ouvertes
```

`agent-runner-lg init` le propose par defaut, avec le compte du jeton de lecture.

Une liste **vide** prend toutes les PR. C'est le bon reglage dans un seul cas :
un demon **unique**, partage par une equipe, tournant sous une identite de
service. Des qu'il y en a deux sur les memes depots, il faut la renseigner — et
le symptome d'un oubli (deux reponses identiques dans un fil) ne designe pas la
cause.

### Ce que chaque developpeur installe

```bash
git clone git@github.com:dlasserre/reviewer.git
cd reviewer
python -m venv .venv && .venv/bin/pip install -e ".[keyring]"
.venv/bin/agent-runner-lg init
```

L'assistant demande ses jetons, les verifie contre la forge, liste les depots
qu'ils voient, et ecrit une configuration a son nom. Les fichiers produits ne
sont pas versionnes : chacun a la sienne.

### Deux points a trancher en equipe

Le perimetre ne les regle pas, et ils changent ce que voient les autres.

| | |
|---|---|
| **Sous quel compte l'agent ecrit** | aujourd'hui le PAT de chacun : les commits et les reponses portent le nom du developpeur. Cinq developpeurs, cinq identites sur les memes depots. Une GitHub App ou un compte de service donnerait une identite unique — mais c'est un autre mecanisme de jeton |
| **Quel quota Claude** | `CLAUDE_CODE_OAUTH_TOKEN` est un abonnement **personnel**. Plusieurs demons dessus le vident, et la personne dont c'est le compte le decouvre en se faisant limiter. Une cle API reglerait ca, mais la consommation bascule sur la facturation a l'usage |

### Ce qui ne se partage pas

- **La base d'etat** (`state_db`) : baux, curseurs de remarques, cycles. Deux
  demons qui la partageraient sans partager le reste se croiraient mutuellement
  en train de travailler.
- **Les worktrees** : un arbre par PR, derive d'un clone local.
- **Le port de la console** : 8788 par defaut. Deux demons sur une meme machine
  en demandent deux.

## Ce qui change, et ce qui ne change pas

| | |
|---|---|
| **Reecrit** | `job.py` (1 100 l.) et `reconcile.py` (240 l.) deviennent un graphe de onze noeuds |
| **Deplace tel quel** | tout le reste — les regles, la forge, le prompt, le garde-fou, les worktrees, git, les verifications, les textes, le journal, l'API, la console |
| **Inchange** | le **Claude Agent SDK**. C'est lui qui ecrit le code. LangGraph decide quand l'appeler et quoi faire du resultat |

Les briques deplacees le sont **verbatim** : meme code, memes commentaires, memes
invariants. Seuls leurs imports sont reecrits, en **absolu**
(`from agent_runner_lg.rules.machine import ...`) — avec sept dossiers, un
`from ..rules.machine import` obligerait a compter les niveaux pour savoir d'ou
vient une brique.

## Les sept briques

Une regle : **un dossier repond a une question.**

| Dossier | La question | Contenu |
|---|---|---|
| `graph/` | **qui orchestre ?** | l'etat, les noeuds, le cablage |
| `rules/` | **qui decide ?** | `decide()` et le verdict — pur, aucun reseau |
| `forge/` | **qui parle a GitHub ?** | lecture, ecriture, gestes composes |
| `agent/` | **qui ecrit le code ?** | le SDK, son prompt, son garde-fou |
| `repo/` | **qui touche au disque ?** | worktree, git, verifications |
| `store/` | **qu'est-ce qu'on garde ?** | les baux, les points de reprise |
| `output/` | **qu'est-ce qu'on dit ?** | reponses GitHub, journal, API, console |

`config.py` reste a la racine : il est lu par les sept.

## Le graphe

```
observe ──> decider ──┬── rien a faire ──────────────────────────> FIN
                      ├── prevenir ────> notify ─────────────────> FIN
                      └── travailler ──> admit ──┬── refuse ─────> FIN
                                                 └── admis ──> plan
                                                                │
    ┌───────────────────────────────────────────────────────────┘
    ├── observation seule ──> dry_run ─────────────────────────> FIN
    └── coder ──> code ──┬── arret ──────────────────────> settle
                         └── juger ──> judge ──┬── arret ─> settle
                                               ├── sans diff ──> speak
                                               └── verifier ──> verify
                                                                  │
                ┌─────────────────────────────────────────────────┘
                ├── rouge ────────────────────────────────> settle
                └── vert ──> publish ──> speak ──> settle ──────> FIN
```

| Noeud | Ce qu'il fait |
|---|---|
| `observe` | relit la forge et l'etat local |
| `decider` | applique les regles — fonction pure, aucun effet |
| `admit` | le portier : budget du jour, puis bail |
| `plan` | l'issue de rattachement, la branche, le prompt |
| `code` | **le Claude Agent SDK** |
| `judge` | confronte ce que l'agent dit a ce que l'arbre montre |
| `verify` | lance les verifications du depot |
| `publish` | commit, push, PR derivee |
| `speak` | repond dans les fils, resout ce qui est solde |
| `settle` | ecrit l'etat, rend le bail, dit le mot de la fin |
| `notify` | la voie sans agent : prevenir, et s'arreter |

### Trois proprietes qu'on ne change pas

1. **Un seul cycle, jamais de boucle.** Le graphe corrige, publie, s'arrete.
   C'est la reconciliation SUIVANTE qui redecide au vu du nouvel etat. Une
   boucle interne casserait le declenchement sur niveau : le graphe raisonnerait
   sur un etat qu'il croit connaitre au lieu de le relire.

2. **Toutes les sorties passent par `settle`** — sauf celles ou aucun bail n'a
   ete pris (`observe` sans PR, `admit` qui refuse) et le mode observation.
   `settle` est le `finally` : il rend le bail quoi qu'il arrive.

3. **Le fil de checkpoint est le JOB, pas la PR.** `thread_id = job_id`. Un fil
   par PR ferait reprendre le cycle suivant la ou le precedent s'etait arrete.
   Le `job_id` d'un cycle interrompu est ecrit dans le bail : `store.sweep_dead()`
   le rend.

## Ce que LangGraph apporte reellement

- **La reprise.** Un point de sauvegarde apres chaque noeud. Un arret entre
  `publish` et `speak` reprend a `speak` — aujourd'hui, les reponses sont
  perdues et le cycle entier se rejoue.
- **La lisibilite.** La sequence tient dans un schema. Elle etait repartie dans
  700 lignes de `run()`.
- **Le flux.** Les transitions de noeuds alimentent la console sans cablage
  supplementaire.
- **Le rejeu par noeud.** Une `ForgeError` passagere sur `speak` se retente sans
  relancer l'agent.

### Ce qu'il n'apporte PAS, contrairement a ce qu'on pourrait croire

`interrupt()` **ne remplace pas** le marqueur `ASK_MARK`. Quand le demon pose
une question dans un fil, le signal de reprise arrive **sur GitHub**, pas par le
graphe : un `interrupt()` attendrait une reprise que personne ne peut lui
envoyer, en gardant un fil de checkpoint ouvert indefiniment. Le declenchement
sur niveau repond deja — on previent, on s'arrete, le passage suivant lit la
reponse.

Le seul usage juste d'`interrupt()` ici serait **l'approbation du diff avant
push** : suspendre apres `verify`, montrer le diff dans la console, reprendre
sur un clic. Non cable pour l'instant.

## Deux differences assumees avec le runner d'origine

Tout le reste est un portage a l'identique. Ces deux points sont des
changements de comportement, choisis :

1. **Le graphe relit la PR avant d'agir.** L'original agit sur la photo prise
   par le balayage — qui peut avoir vingt-cinq minutes quand les vagues
   precedentes ont pris du temps. Ici, `observe` relit. Cout : une requete
   GraphQL par job.

2. **`writes_enabled: false` bloque AUSSI la voie « prevenir l'humain ».** Dans
   l'original, le garde de lecture seule est place apres la construction du
   prompt, et la branche `ASK_HUMAN` sort avant de l'atteindre : le mode annonce
   comme « lit, decide, s'arrete » publie des commentaires. Ici le garde vient
   d'abord — et c'est indispensable tant que les deux demons lisent les memes PR.

## Etat

- [x] Briques deplacees (17 modules, imports absolus, verifies)
- [x] Le graphe : etat, dependances, noeuds, cablage
- [x] Gestes composes sur la forge (`forge/actions.py`)
- [x] Balayage et ordonnancement (`graph/sweep.py`)
- [x] La CLI : `check`, `status`, `run`, `serve`
- [x] `check` et `status` valides sur les vraies PR d'Insectorize
- [x] **585 tests, 0 echec** — dont les 50 de `test_job.py`, portes sur le graphe
- [x] `scripts/comparer.py` : les deux arbres portent les memes regles
- [x] Console : le graphe en direct, l'endroit ou chaque job se trouve dedans
- [ ] **Le graphe n'a pas encore tourne de bout en bout sur une vraie PR.** Aucune
      des PR ouvertes ne demandait d'agent au moment des essais. La sequence
      complete est couverte par les tests, pas par une execution reelle.
- [ ] Armer (`writes_enabled: true`) — pas avant ce point

## La console

`http://127.0.0.1:8788` quand `serve` tourne. Port DIFFERENT de celui du runner
d'origine (8787), pour que les deux cohabitent.

Le graphe est central : il montre ou en est le cycle SELECTIONNE dans la liste
de gauche. Cette liste porte l'historique — un cycle par ligne, avec son depot,
sa PR et son statut (`en cours`, `termine`, `arbitrage`, `echec`, `a blanc`,
`interrompu`).

| Bouton | Ce qu'il fait |
|---|---|
| **Condense** (defaut) | la disposition se CALCULE pour la place disponible : l'epine se replie en boustrophedon, et le nombre de colonnes est celui qui remplit le mieux |
| **Vertical / Horizontal** | les deux mises en page figees, si on les prefere |
| **Navigation** | molette pour zoomer au curseur, glisser pour deplacer, `Ajuster` ou double-clic pour recadrer |
| **Test** | simule un cycle ENTIEREMENT dans le navigateur — n'appelle rien, n'ecrit rien, statut « test » |
| **Journal** | le journal du demon, dans une modale |
| **Vider** | MASQUE les cycles passes. N'efface RIEN sur le disque |

### Ce qui la rend possible, et qui est dans le code

1. **`graph.node`** — un evenement par ENTREE de noeud, emis par `build.py`.
   L'entree, pas la sortie : un evenement de sortie n'arrive qu'une fois le
   noeud fini, et la console resterait figee sur l'etape precedente pendant
   toute la duree de `code`.
2. **`GET /graph`** — la topologie lue du graphe COMPILE. Le dessin ne peut donc
   pas mentir : un schema recopie dans le front derive du cablage a la premiere
   modification, et rien ne le signale.
3. **`GET /history`** — les cycles passes, reconstitues depuis les FICHIERS de
   journal. Le statut n'est pas stocke, il se DEDUIT des evenements : un statut
   ecrit quelque part serait une seconde verite, qui finirait par contredire le
   journal.
4. **La mise en page seule vit dans la page.** Ou tombe chaque noeud est une
   decision visuelle ; ce que sont les noeuds et les arcs vient du serveur.

### Le mode condense

Les deux mises en page figees subissent le rapport de forme du panneau : un
graphe 440 x 800 dans un panneau large et bas n'occupe qu'un quart de la surface,
quoi qu'on fasse. Le condense, lui, se calcule — on essaie chaque nombre de
colonnes et on garde celui qui remplit le mieux.

L'epine (`observe` -> `settle`) se replie en **boustrophedon** : une rangee vers
la droite, la suivante vers la gauche. La propriete qui rend ce pliage simple,
c'est que le dernier noeud d'une rangee et le premier de la suivante tombent dans
la MEME colonne — le passage d'une rangee a l'autre est un trait vertical, pas un
detour. Les derivations (`notify`, `dry_run`) occupent une voie au-dessus de leur
rangee.

Trois regles de trace suffisent : meme rangee -> trait horizontal ; meme colonne
-> trait vertical ; tout le reste -> courbe passant sous les deux noeuds. La
troisieme ne sert qu'aux arrets et au contournement, traces faibles jusqu'a ce
qu'ils soient pris — les dessiner tous en clair ferait un plat de spaghettis,
les cacher ferait mentir le dessin.

Occupation mesuree, condense contre le meilleur des deux figes :

| Panneau | Condense | Fige |
|---|---|---|
| 880 x 470 | 5 col x 2 rang, 76 % | 51 % |
| 1400 x 620 | 5 col x 2 rang, 92 % | 61 % |
| 520 x 380 | 4 col x 3 rang, 98 % | 40 % |
| 400 x 900 | 2 col x 5 rang, 98 % | 81 % |

### Le cadrage

Le SVG occupe tout le panneau et son `viewBox` vaut sa taille EN PIXELS ; tout le
cadrage est porte par la transformation du groupe `#cam`. Le premier jet laissait
le SVG se dimensionner sur son `viewBox` : un graphe horizontal (1590 x 430)
rendu dans un panneau de 880 x 750 occupait 240 px de haut, un quart de la
surface, et rien ne remplissait les trois autres quarts.

Le mode par defaut est le CONDENSE, et il n'est pas memorise : il se recalcule a
chaque changement de taille, et redessine seulement quand le nombre de colonnes
change vraiment. Un clic sur `Condense`, `Vertical` ou `Horizontal`, lui, est
retenu.

### Trois pieges deja rencontres

- **Les evenements arrivent en double.** Le flux SSE rejoue ses derniers
  evenements a la connexion, `/jobs` rend les memes et `/history` couvre la meme
  periode. Sans cle de deduplication, UN cycle s'affichait deux ou trois fois de
  suite — comme si le demon l'avait rejoue.
- **`charger()` doit DECLARER ce qu'il charge.** Le fil complet d'un cycle est
  charge a la demande depuis `/history/{job_id}` ; sans enregistrer ses
  empreintes, les memes evenements revenaient par le flux et par `/jobs` et
  s'empilaient une seconde fois. C'est le doublement qui survivait a la premiere
  correction.
- **`agent.step` et `graph.node` sont ecartes du journal general.** Un job mesure
  produit 60 a 120 etapes contre une dizaine de transitions ; melanges, la vue
  generale ne dit plus rien de ce que le demon a decide. Les deux restent dans le
  flux et dans le fil d'un cycle.

## Sur la comparaison des deux versions

`scripts/comparer.py` lit la forge UNE FOIS et donne le meme snapshot aux deux
implementations de `decide`. Il attrape une chose, et une seule : un arbre
modifie sans l'autre.

Il ne prouve PAS l'equivalence de comportement, et deux constats l'ont impose :

1. **La premiere version comparait deux `status` lances l'un apres l'autre.**
   Sur `backend#727`, dont la CI tournait, un lancement rendait
   `origine=WAITING_REVIEW, portage=WAITING_CI` et le suivant exactement
   l'INVERSE. L'ecart s'inversait avec l'ordre de lecture : c'etait la PR qui
   bougeait, pas une regle qui differait.

2. **`machine.py` est identique au caractere pres** dans les deux arbres — il
   n'importe rien du paquet. Comparer deux `decide` qui sont le meme code ne
   pouvait rien apprendre.

Ce qui repond de l'orchestration — le seul morceau reellement reecrit — ce sont
les tests.
