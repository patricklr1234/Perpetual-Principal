#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Perpetual Principal production entrypoint v70.

Completes v69 by restoring native protection for a state leg recovered from the
durable ledger. v69 correctly restored ownership/accounting without touching
orders, but production proof showed ETH SHORT physical/state/ledger=0.009 while
native STOP/TP coverage remained only 0.002.

v70 installs protection ONLY for the restored missing RANGE leg, using the
owning live basket's already-persisted recovery TP/SL targets. Existing orders
and other strategies are untouched. If either protection cannot be confirmed,
the strategy remains fail-closed.
"""

import runner_v69 as base

bot = base.bot
_original_reconcile_v69 = bot.Reconciler.reconcile
_done_v70 = set()


def _protect_restored_live_lots_v70(self):
    snap = self.snapshot()
    state_ids = self._represented_state_leg_ids()
    repaired = []
    # Durable open lots that are represented in state but whose own owner has no
    # active native order coverage are candidates. Limit strictly to v69-restored legs.
    with self.store.lock:
        grids = dict(self.store.state.get("range_grids", {}) or {})
    for key, st in grids.items():
        if not isinstance(st, dict) or st.get("last_result") != "STATE_LIVE_LOT_RESTORED_V69":
            continue
        symbol = str(st.get("symbol") or str(key).split(":")[0]).upper()
        sid = str(st.get("strategy") or f"RANGE:{key}")
        basket = st.get("basket") or {}
        for leg in basket.get("legs", []) or []:
            if str(leg.get("reason") or "") != "STATE_LIVE_LOT_RESTORE_V69":
                continue
            leg_id = str(leg.get("id") or "")
            if not leg_id or leg_id in _done_v70 or leg_id not in state_ids:
                continue
            side = str(leg.get("side") or "").upper(); qty = bot.dec(leg.get("qty"))
            if qty <= 0:
                continue
            # Do not duplicate if this exact strategy already has enough native coverage.
            stop_cov = bot.D(0); tp_cov = bot.D(0)
            for o in snap.open_orders or []:
                if str(o.get("symbol") or "").upper()!=symbol or str(o.get("positionSide") or "").upper()!=side:
                    continue
                cid=str(o.get("clientOrderId") or ""); owner=self.ledger.order_owner(cid) if cid else None
                if not owner or not (owner == sid or owner.startswith(sid + ":")):
                    continue
                rem=max(bot.D(0),bot.dec(o.get("origQty"))-bot.dec(o.get("executedQty")))
                typ=str(o.get("type") or "").upper()
                if typ=="STOP_MARKET": stop_cov+=rem
                elif typ=="TAKE_PROFIT_MARKET": tp_cov+=rem
            state_owner_qty=sum((bot.dec(x.get("qty")) for x in basket.get("legs",[]) if str(x.get("side") or "").upper()==side),bot.D(0))
            if stop_cov >= state_owner_qty and tp_cov >= state_owner_qty:
                _done_v70.add(leg_id); continue

            tp=bot.dec(basket.get("recovery_tp_price") or basket.get("tp_price"))
            sl=bot.dec(basket.get("recovery_stop_price") or basket.get("hard_stop_price"))
            if tp<=0 or sl<=0:
                self.store.set_protection_block(sid, f"V70_NO_PROTECTION_TARGETS:{leg_id}")
                continue
            block=f"V70_RESTORED_LEG_PROTECTION:{sid}"
            self.store.set_operational_block(block,f"installing native protection for restored leg {leg_id}")
            try:
                # install_basket_exit can protect a single leg and records durable order ownership.
                tpmeta=self.exe.install_basket_exit(sid+":V70:TP",symbol,[leg],tp,self.client.mark(symbol))
                slmeta=self.exe.install_basket_exit(sid+":V70:SL",symbol,[leg],sl,self.client.mark(symbol))
                if not tpmeta or not slmeta:
                    raise RuntimeError("native protection not confirmed")
                self.store.set_protection_block(sid,None)
                self.store.set_operational_block(block,None)
                _done_v70.add(leg_id)
                repaired.append((sid,leg_id,qty,tp,sl))
                bot.logger.warning(
                    "RANGE RESTORED LEG PROTECTION V70 | CONFIRMED | strategy=%s leg=%s qty=%s TP=%s SL=%s | positions=UNTOUCHED existing_orders=UNTOUCHED",
                    sid,leg_id,qty,tp,sl)
            except Exception as exc:
                self.store.set_protection_block(sid,f"V70_PROTECTION_FAILED:{leg_id}:{exc}")
                self.store.set_operational_block(block,f"V70_PROTECTION_FAILED:{exc}")
                bot.logger.exception("RANGE RESTORED LEG PROTECTION V70 FAILED | %s | fail_closed=True",sid)
    return repaired


def _reconcile_v70(self,*args,**kwargs):
    ok=_original_reconcile_v69(self,*args,**kwargs)
    if ok:
        try:
            _protect_restored_live_lots_v70(self)
        except Exception:
            bot.logger.exception("RANGE RESTORED LEG PROTECTION V70 | fail_closed=True")
    return ok


bot.Reconciler.reconcile=_reconcile_v70
bot.VERSION=f"{bot.VERSION}-restored-leg-native-protection-v70"


def main():
    bot.logger.warning("RANGE RESTORED LEG NATIVE PROTECTION V70 ACTIVE | restored_v69_only=True | existing_orders=UNTOUCHED positions=UNTOUCHED fail_closed=True")
    base.main()


if __name__=="__main__":
    main()
