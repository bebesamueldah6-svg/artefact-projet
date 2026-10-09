import pytest

from edan_chat.agent.entities import resolve
from edan_chat.ingest.normalize import norm_key, party_key, short_locality


def test_normalization_rules():
    assert norm_key("Côte d’Ivoire") == "COTE D IVOIRE"
    assert party_key("R.H.D.P") == party_key("rhdp") == "RHDP"
    assert short_locality("BAZRA NATILS, DANANON, KETRO-BASSAM ET VAVOUA, COMMUNES ET SOUS-PREFECTURES") == [
        "BAZRA NATILS", "DANANON", "KETRO BASSAM", "VAVOUA"]


@pytest.mark.parametrize("question, kind, value", [
    ("Qui a gagné à yopougon ?", "circonscription", "YOPOUGON"),
    ("Who won in Tiapum?", "circonscription", "TIAPOUM"),            # typo
    ("participation à Agbovile", "circonscription", "AGBOVILLE"),     # typo
    ("score de koffi aka charles", "candidate", "KOFFI AKA CHARLES"),
    ("sièges du PDCI", "party", "PDCIRDA"),
    ("Seats won by R.H.D.P", "party", "RHDP"),                        # punctuation alias
    ("Rassemblement des Houphouëtistes pour la Démocratie et la Paix", "party", "RHDP"),  # long form
    ("indépendants élus", "party", "INDEPENDANT"),
    ("région du Poro", "region", "PORO"),
])
def test_resolve(question, kind, value):
    assert any(m.kind == kind and m.value == value for m in resolve(question))


def test_ambiguous_localities():
    (bouake,) = resolve("Qui a gagné à Bouaké ?")
    assert bouake.ambiguous and bouake.circ_ids == ["060", "061"]
    (bassam,) = resolve("Résultats à Bassam")
    assert bassam.ambiguous and {a["value"] for a in bassam.alternatives} == {"GRAND BASSAM", "KETRO BASSAM"}
    (grand_bassam,) = resolve("Top 5 in Grand-Bassam")
    assert not grand_bassam.ambiguous


def test_generic_questions_have_no_entities():
    assert resolve("Quel parti a le plus de sièges ?") == []
    assert resolve("Participation rate by region") == []
