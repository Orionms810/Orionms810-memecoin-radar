"""Réglages du trading automatique (section « trading » de config.toml)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TradingSettings:
    # off = pas de trading · paper = argent virtuel (par défaut) · live = vrai argent
    mode: str = "paper"
    paper_start_sol: float = 0.05  # ~7 $ de capital virtuel

    # Taille des positions
    reserve_sol: float = 0.008  # jamais tradé : frais réseau + loyer des comptes token
    position_pct: float = 0.35  # part du capital disponible engagée par trade
    min_position_sol: float = 0.005  # en dessous, les frais mangent tout : on ne trade pas
    max_position_sol: float = 0.5
    max_open_positions: int = 1

    # Filtres d'entrée (plus stricts que les alertes)
    min_safety: int = 60
    min_momentum: int = 30
    # Réglés sur 2 jours de données (631 tokens) : au-delà, la plupart s'effondraient
    max_entry_mcap_sol: float = 50.0  # au-delà, on arrive trop tard
    max_entry_age_seconds: float = 90.0
    max_dev_buy_pct: float = 5.0  # dev ≥ 5 % : 59 % des tokens finissaient à moins de x0,5
    max_top10_pct: float = 30.0  # top 10 ≥ 30 % : 68 % des tokens finissaient à moins de x0,5

    # Sorties AVANT x2 : protection serrée. Sur 2 jours de données, aucun des 179 tokens
    # tombés à -25 % ou pire en 5 min n'est remonté au-dessus du prix d'entrée en 30 min.
    stop_loss_pct: float = 25.0
    take_profit_pct: float = 100.0  # à +100 % (x2)...
    take_profit_fraction: float = 0.5  # ...on vend la moitié (on récupère sa mise)
    trailing_arm_pct: float = 40.0  # le trailing stop s'active à +40 %...
    trailing_stop_pct: float = 30.0  # ...et vend tout si le prix recule de 30 % depuis son plus haut
    time_stop_minutes: float = 15.0  # sortie si le trade ne décolle pas...
    time_stop_min_gain_pct: float = 10.0  # ...(moins de +10 % après 15 min)
    stale_minutes: float = 5.0  # sortie si plus aucun échange sur le token

    # Sorties APRÈS x2 (« moonbag ») : la mise est récupérée, le reste peut courir
    take_profit2_pct: float = 400.0  # à +400 % (x5)... (0 = pas de 2e palier)
    take_profit2_fraction: float = 0.5  # ...on revend la moitié de ce qui reste
    moonbag_trailing_pct: float = 45.0  # trailing large : les vrais runs reculent souvent de 30-40 %
    # Sortie si les vendeurs prennent le dessus : sur la dernière minute, ventes ≥ 2x les
    # achats (et ≥ 1 SOL), alors que le prix est déjà 20 % sous son plus haut
    reversal_window_seconds: float = 60.0  # 0 = désactivé
    reversal_sell_ratio: float = 2.0
    reversal_min_sell_sol: float = 1.0
    reversal_drop_pct: float = 20.0
    moonbag_stale_minutes: float = 60.0  # vend si plus aucun échange pendant 1 h
    moonbag_max_hours: float = 72.0  # durée maximale de garde (0 = illimitée)
    max_moonbags: int = 3  # moonbags qui ne bloquent pas les nouveaux achats

    # Suivi après la vente : le prix est encore relevé 2 h pour vérifier si on sort trop tôt
    after_watch_minutes: float = 120.0
    curve_check_seconds: float = 20.0  # relecture de la courbe si aucun trade reçu depuis 20 s

    # Coupe-circuits
    daily_loss_limit_pct: float = 35.0  # stop pour la journée si -35 % depuis minuit (UTC)
    max_drawdown_pct: float = 60.0  # arrêt total si -60 % depuis le plus haut (relance : /trading on)
    max_consecutive_losses: int = 3
    cooldown_minutes: float = 120.0  # pause après N pertes d'affilée

    # Exécution
    slippage_pct: float = 15.0
    priority_fee_sol: float = 0.0003
    wallet_max_balance_sol: float = 0.5  # refuse le mode réel si le wallet contient plus (anti-erreur)
    fee_pct: float = 1.75  # frais estimés par ordre en mode papier (pump.fun + PumpPortal)
    tx_fee_sol: float = 0.0003  # frais réseau estimés par transaction en mode papier
    paper_latency_seconds: float = 2.0  # délai simulé entre la décision et l'exécution
