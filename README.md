# edan-chat — Questions-réponses sur les législatives ivoiriennes 2025

Posez des questions en français sur les résultats officiels des élections législatives
(EDAN 2025) publiés par la CEI, et obtenez une réponse chiffrée, sourcée (page du PDF),
avec le tableau et la requête SQL qui l'ont produite.

> « Qui a gagné à Yopougon ? » · « Combien de sièges pour le PDCI ? » ·
> « Quelle région a la plus forte participation ? »

Tout tourne en local et aucune donnée ne quitte l'ordinateur. Deux moteurs de réponse :

| Moteur | Réglage | Vitesse | Couverture |
|---|---|---|---|
| **Règles fixes** (par défaut) | `ENGINE=rules` | < 0,1 s | questions types (voir ci-dessous) |
| Modèle de langage local (Ollama) | `ENGINE=llm` | 20–45 s sur CPU | questions libres |

## Installation

Prérequis : [uv](https://docs.astral.sh/uv/). [Ollama](https://ollama.com) seulement pour `ENGINE=llm`.

```bash
uv sync
uv run python -m edan_chat.ingest      # télécharge le PDF, construit la base, vérifie
ollama pull qwen2.5:7b                 # optionnel, ~4,7 Go, uniquement pour ENGINE=llm
```

## Utilisation

```bash
uv run streamlit run src/edan_chat/app.py   # interface web (graphiques, tableaux, SQL)
uv run edan-chat                            # chat dans le terminal
```

Configuration par variables d'environnement ou fichier `.env` (voir `src/edan_chat/config.py`) :
`LLM_MODEL`, `OLLAMA_HOST`, `SQL_MAX_ROWS`, `SQL_TIMEOUT_S`…

## Questions comprises par le moteur à règles

Le moteur reconnaît le type de question par mots-clés et les lieux / partis / candidats par
correspondance approchée (fautes et accents tolérés), puis exécute une requête SQL prédéfinie.

| Type | Exemples |
|---|---|
| Sièges par parti | « Combien de sièges a obtenu chaque parti ? » |
| Un parti | « Combien de sièges pour le FPI ? » · « Combien d'indépendants ont été élus ? » |
| Parti dans une zone | « Combien de sièges pour le RHDP dans le Poro ? » |
| Élu d'une circonscription | « Qui a gagné à Yopougon ? » |
| Résultats détaillés | « Résultats à Cocody » |
| Élus d'une région | « Qui a gagné dans la région du Poro ? » |
| Participation | « Taux de participation national » · « Participation à Bouaké » |
| Classements | « Les 5 circonscriptions avec la plus faible participation » · « Participation par région » |
| Candidat | « Score de Koffi Aka Charles » |
| Meilleurs scores | « Quel élu a obtenu le meilleur pourcentage ? » |
| Suivi de conversation | « Résultats à Cocody » puis « et à Abobo ? » |

Les questions hors périmètre (autres élections, prévisions) et les demandes de modification
sont refusées ; une question non reconnue reçoit une aide avec des exemples.
Ajouter un type de question = ajouter une fonction `i_...` dans `agent/rules.py`.

## Fonctionnement du mode LLM (`ENGINE=llm`)

```
PDF CEI ──ingest──▶ DuckDB (205 circonscriptions, 1 125 candidatures) + 9 contrôles de cohérence
                         ▲
question ─▶ entités ─▶ LLM : SQL ─▶ garde-fou ─▶ exécution ─▶ LLM : réponse rédigée
            (lieux,       (ou refus /     │  (lecture seule,      (uniquement à partir
             candidats,    précision)     │   200 lignes, 5 s)     des résultats)
             partis)                      └─ erreur ⇒ 1 correction automatique
```

1. **Ingestion** (`ingest/`) : extraction géométrique du PDF, normalisation des noms, export
   Parquet/CSV/DuckDB et manifeste versionné (hash du PDF). Les contrôles vérifient
   l'arithmétique : votants = nuls + exprimés, somme des voix = exprimés − blancs, un élu par
   circonscription, totaux nationaux identiques à ceux du PDF, etc.
2. **Résolution d'entités** (`agent/entities.py`) : « yopougon », « Agbovile » (faute),
   « PDCI » sont reliés aux identifiants de la base par correspondance approchée, et passés au
   modèle comme indices.
3. **Génération SQL** (`agent/prompts.py`) : le modèle ne voit que des vues documentées
   (`vw_winners`, `vw_turnout`, `vw_party_summary`…) et peut aussi refuser une question hors
   périmètre ou demander une précision.
4. **Garde-fou** (`agent/sql_guard.py`) : une seule requête `SELECT`, liste blanche de vues,
   fonctions d'accès fichiers/réseau interdites, plafond de lignes, base ouverte en lecture
   seule sans accès externe, délai maximal.
5. **Réponse** : rédigée à partir des seules lignes renvoyées ; l'interface affiche aussi le
   tableau, le SQL et les pages sources du PDF.
6. **Traçabilité** : chaque échange est enregistré dans `traces/AAAA-MM-JJ.jsonl`.

## Qualité

```bash
uv run pytest                          # tests unitaires (dont les 20 questions sur le moteur à règles)
uv run python -m edan_chat.eval        # évaluation du moteur configuré (ENGINE)
```

Résultats : moteur à règles 20/20 ; qwen2.5:7b 13/14 sur la première série.
L'évaluation pose 20 questions (chiffres nationaux, lieux avec fautes, classements, questions
hors sujet, tentative de suppression) dont les réponses attendues ont été calculées dans la base.

## Limites

- Moteur à règles : seules les formulations prévues sont comprises ; une question inhabituelle
  reçoit le message d'aide plutôt qu'une réponse.
- Le modèle local peut mal interpréter une question formulée de façon inhabituelle : vérifiez
  le tableau et le SQL affichés sous chaque réponse.
- Seules les données du PDF national détaillé sont disponibles (pas de résultats par bureau
  de vote, pas d'autres scrutins).
- `is_list` (liste vs candidat individuel) est déterminé par une heuristique sur le nom.
