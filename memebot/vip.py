"""Snipes : acheter tout de suite, sans attendre l'observation habituelle.

Deux déclencheurs, parce que c'est là que sont les gros multiples :
- un compte de confiance (une chaîne Telegram qui republie les posts d'une
  personne connue) poste l'adresse d'un token (le « CA ») ;
- un créateur que tu suis (son wallet) lance un nouveau token.

Le danger n°1 : les faux. N'importe qui peut créer un token « Elon » avec le lien
de son compte X. Seule l'adresse postée par le VRAI compte, ou le VRAI wallet du
créateur, compte. Les adresses vues ailleurs sont ignorées.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from .chainfeed import _B58, b58encode

PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
CREATOR_ID = "CREATEUR"  # pseudo-narratif : créateur qui a déjà fait graduer un token


@dataclass
class SnipeSettings:
    enabled: bool = True
    trade: bool = True  # acheter (papier ou réel selon [trading]) ; false = alerte seule
    max_mcap_sol: float = 2000.0  # pas d'achat au-delà (~300 000 $)
    extra_slots: int = 1  # un snipe peut s'ajouter à une position déjà ouverte
    # Créateurs qui ont déjà fait graduer un token : leurs nouveaux tokens sont
    # analysés comme une alerte (observation + contrôles anti-rug)...
    proven_creators: bool = True
    proven_trade: bool = False  # ...mais pas achetés tant que /analyse n'a pas montré que ça marche
    proven_max_per_hour: int = 10  # analyses par heure au maximum (crédits RPC)


# Adresse Solana : 32 à 44 caractères base58, isolée (pas au milieu d'un mot)
_ADDR_RE = re.compile(r"(?<![1-9A-HJ-NP-Za-km-z])[1-9A-HJ-NP-Za-km-z]{32,44}(?![1-9A-HJ-NP-Za-km-z])")
_B58_INDEX = {c: i for i, c in enumerate(_B58)}


def b58decode(s: str) -> bytes:
    n = 0
    for c in s:
        n = n * 58 + _B58_INDEX[c]
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    pad = len(s) - len(s.lstrip("1"))
    return b"\0" * pad + body


def is_pubkey(s: str) -> bool:
    try:
        return len(b58decode(s)) == 32
    except KeyError:
        return False


def extract_mints(text: str) -> list[str]:
    """Adresses de tokens dans un post. Les adresses pump.fun (qui finissent par
    « pump ») passent en premier."""
    seen: dict[str, None] = {}
    for m in _ADDR_RE.findall(text or ""):
        if m not in seen and is_pubkey(m):
            seen[m] = None
    return sorted(seen, key=lambda a: not a.endswith("pump"))


# ------------------------------------------------- adresse de la bonding curve
# Une adresse dérivée de programme (PDA) ne doit pas être un point de la courbe
# ed25519 : on essaie les « bumps » de 255 à 0, comme Solana.
_P = 2**255 - 19
_D = (-121665 * pow(121666, _P - 2, _P)) % _P


def _on_curve(b: bytes) -> bool:
    y = (int.from_bytes(b, "little") & ((1 << 255) - 1)) % _P
    y2 = y * y % _P
    u = (y2 - 1) % _P
    v = (_D * y2 + 1) % _P
    x2 = u * pow(v, _P - 2, _P) % _P
    return x2 == 0 or pow(x2, (_P - 1) // 2, _P) == 1


def find_program_address(seeds: list[bytes], program_id: str) -> str:
    program = b58decode(program_id)
    for bump in range(255, -1, -1):
        h = hashlib.sha256(b"".join(seeds) + bytes([bump]) + program + b"ProgramDerivedAddress").digest()
        if not _on_curve(h):
            return b58encode(h)
    raise ValueError("aucune adresse dérivée trouvée")


def bonding_curve_address(mint: str) -> str:
    return find_program_address([b"bonding-curve", b58decode(mint)], PUMP_PROGRAM)
