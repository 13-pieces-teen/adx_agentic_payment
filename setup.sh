#!/bin/bash
# ADX Agent Arena — Project Setup
set -e
echo "============================================"
echo " ADX Agent Arena — Setup"
echo "============================================"
echo "[1/3] Configuring git hooks..."
git config core.hooksPath .githooks
echo "  ✅ Git hooks path set to .githooks/"
echo "[2/3] Checking Python..."
echo "  ✅ Python $(python3 --version 2>&1 | awk '{print $2}')"
echo "[3/3] Verifying matching module..."
python3 -c "
import sys; sys.path.insert(0, '.')
from matching import (
    AgentRegistry, AgentRegistration,
    Arena, OrderBook, ResourceListing,
    NegotiationProtocol, Intent, IntentType, AssetClass,
    PriceConstraint, build_agent_context,
)
# Smoke test: register agents → list resource → match → negotiate → arena
reg = AgentRegistry()
book = OrderBook()
book.configure(reg)
arena = Arena(reg)
nego = NegotiationProtocol(arena=arena)

# Register agents
buyer = AgentRegistration(name='BuyerBot', negotiation_style='balanced',
    tradable_assets=['compute'], trade_direction='buy')
seller = AgentRegistration(name='SellerBot', negotiation_style='balanced',
    tradable_assets=['compute'], trade_direction='sell')
reg.register(buyer)
reg.register(seller)

# List resource
listing = ResourceListing(seller_agent_id=seller.agent_id, seller_name=seller.name,
    asset_class=AssetClass.COMPUTE, title='A100 GPU Hours', quantity=10)
book.publish_listing(listing)

# Create intents
buy = Intent(agent_id=buyer.agent_id, agent_name=buyer.name,
    intent_type=IntentType.BUY, asset_class=AssetClass.COMPUTE,
    description='Need A100 compute', quantity=10)
buy.price.min_acceptable = 60; buy.price.ideal = 80; buy.price.max_acceptable = 100

sell = Intent(agent_id=seller.agent_id, agent_name=seller.name,
    intent_type=IntentType.SELL, asset_class=AssetClass.COMPUTE,
    description='A100-80GB, 99.9% uptime', quantity=10)
sell.price.min_acceptable = 70; sell.price.ideal = 90; sell.price.max_acceptable = 110

book.publish(buy); book.publish(sell)

# Match
matches = book.find_matches(buy)
print(f'  ✅ {len(matches)} match(es) found, score={matches[0].score:.1f}')

# Negotiate (simulate)
from matching.negotiation import Proposal, ProposalType
session = nego.create_session(matches[0])
p1 = Proposal(proposal_type=ProposalType.INITIAL_OFFER, price=70, quantity=10,
    message='Opening bid: 70 INJ/hr', sender_intent_id=buy.intent_id)
r1 = nego.process_proposal(session, p1, buy)
print(f'  ✅ Round 1: {r1[\"action\"]} — {r1[\"state\"]}')

p2 = Proposal(proposal_type=ProposalType.COUNTER, price=90, quantity=10,
    message='Counter: 90 INJ/hr', sender_intent_id=sell.intent_id)
r2 = nego.process_proposal(session, p2, sell)
print(f'  ✅ Round 2: {r2[\"action\"]} — {r2[\"state\"]}')

# Accept
r3 = nego.accept(session, buy)
print(f'  ✅ Accepted: {r3[\"state\"]} at final_price={r3.get(\"final_price\", \"?\")}')

# Arena stats
print(f'  ✅ Arena stats: {arena.leaderboard.stats}')
print(f'  ✅ Buyer ELO: {buyer.elo_rating:.1f}, Seller ELO: {seller.elo_rating:.1f}')
print('  ✅ ALL SMOKE TESTS PASSED')
"
echo ""
echo "============================================"
echo " Setup complete!"
echo "============================================"
echo "  Run API:  pip install fastapi uvicorn && python3 -c 'from web.api import create_app; import uvicorn; uvicorn.run(create_app(), port=8000)'"
echo "============================================"
