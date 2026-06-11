"""
One-time script: Create a SIP outbound trunk in LiveKit and print the new trunk ID.
Run this on the server where env vars are set, or pass them manually.
"""
import asyncio
import os
import sys
import json

async def main():
    try:
        from livekit import api as lkapi
        from livekit.protocol.sip import (
            CreateSIPOutboundTrunkRequest,
            SIPOutboundTrunkInfo,
            ListSIPOutboundTrunkRequest,
        )
    except ImportError:
        print("ERROR: livekit-api package not installed. Run: pip install livekit-api")
        sys.exit(1)

    lk_url = os.getenv("LIVEKIT_URL", "")
    lk_key = os.getenv("LIVEKIT_API_KEY", "")
    lk_secret = os.getenv("LIVEKIT_API_SECRET", "")

    if not all([lk_url, lk_key, lk_secret]):
        print("ERROR: LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET must be set")
        sys.exit(1)

    sip_domain   = os.getenv("VOBIZ_SIP_DOMAIN", "3e47c8a1.sip.vobiz.ai")
    sip_user     = os.getenv("VOBIZ_USERNAME", "")
    sip_password  = os.getenv("VOBIZ_PASSWORD", "")
    outbound_num = os.getenv("VOBIZ_OUTBOUND_NUMBER", "+918071582929")

    print(f"LiveKit URL  : {lk_url}")
    print(f"SIP Domain   : {sip_domain}")
    print(f"SIP User     : {sip_user}")
    print(f"Outbound Num : {outbound_num}")
    print()

    async with lkapi.LiveKitAPI(url=lk_url, api_key=lk_key, api_secret=lk_secret) as lk:
        # Step 1: List existing trunks
        print("--- Existing outbound trunks ---")
        resp = await lk.sip.list_sip_outbound_trunk(ListSIPOutboundTrunkRequest())
        if resp.items:
            for t in resp.items:
                print(f"  ID={t.sip_trunk_id}  Name={t.name}  Address={t.address}  Numbers={list(t.numbers)}")
        else:
            print("  (none)")
        print()

        # Step 2: Create new outbound trunk
        print("--- Creating new outbound trunk ---")
        trunk_info = SIPOutboundTrunkInfo(
            name="Vobiz Outbound",
            address=sip_domain,
            numbers=[outbound_num],
            auth_username=sip_user,
            auth_password=sip_password,
        )
        req = CreateSIPOutboundTrunkRequest(trunk=trunk_info)
        created = await lk.sip.create_sip_outbound_trunk(req)
        new_id = created.sip_trunk_id
        print(f"  ✅ Created trunk ID: {new_id}")
        print(f"     Name: {created.name}")
        print(f"     Address: {created.address}")
        print(f"     Numbers: {list(created.numbers)}")
        print()
        print(f">>> UPDATE your OUTBOUND_TRUNK_ID to: {new_id}")


if __name__ == "__main__":
    asyncio.run(main())
