# edan-chat — Questions-réponses sur les législatives ivoiriennes 2025

Posez des questions en français sur les résultats officiels des élections législatives
(EDAN 2025) publiés par la CEI, et obtenez une réponse chiffrée, sourcée (page du PDF),
avec le tableau et la requête SQL qui l'ont produite.

> « Qui a gagné à Yopougon ? » · « Combien de sièges pour le PDCI ? » ·
> « Quelle région a la plus forte participation ? »

Tout tourne en local : les données sont dans DuckDB et le modèle de langage (Ollama) est sur
votre machine. Aucune donnée ne quitte l'ordinateur.

## Installation

Prérequis : [uv](https://docs.astral.sh/uv/) et [Ollama](https://ollama.com).

```bash
uv sync
ollama pull qwen2.5:7b                 # ~4,7 Go (ou qwen2.5:3b, plus léger)
uv run python -m edan_chat.ingest      # télécharge le PDF, construit la base, vérifie
```

## Utilisation

```bash
uv run streamlit run src/edan_chat/app.py   # interface web (graphiques, tableaux, SQL)
uv run edan-chat                            # chat dans le terminal
```

Configuration par variables d'environnement ou fichier `.env` (voir `src/edan_chat/config.py`) :
`LLM_MODEL`, `OLLAMA_HOST`, `SQL_MAX_ROWS`, `SQL_TIMEOUT_S`…

## Fonctionnement

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
uv run pytest                          # tests sans LLM (faux modèle scripté)
uv run python -m edan_chat.eval        # évaluation de bout en bout avec le vrai modèle
```

L'évaluation pose 14 questions (chiffres nationaux, lieux avec fautes, classements, questions
hors sujet, tentative de suppression) dont les réponses attendues ont été calculées dans la base.

## Limites

- Le modèle local peut mal interpréter une question formulée de façon inhabituelle : vérifiez
  le tableau et le SQL affichés sous chaque réponse.
- Seules les données du PDF national détaillé sont disponibles (pas de résultats par bureau
  de vote, pas d'autres scrutins).
- `is_list` (liste vs candidat individuel) est déterminé par une heuristique sur le nom.
