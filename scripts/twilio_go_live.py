#!/usr/bin/env python3
"""Move texts to our own verified Twilio toll-free number — every step after the owner's identity check, hands-free.
Until then texts go out through Textbelt's shared pool (TEXTBELT_KEY); Twilio takes over once TWILIO_FROM is set.

Each run does the next step that is due, then exits (launchd com.getduonow.twilio-golive runs it every 30 min):
  1. primary business profile approved?            no → board "user" (owner finishes ID check in the Console)
  2. own a toll-free number (SmsUrl → /twilio/inbound)  buy one if not
  3. live form shows the SMS opt-in (else SMS_SIGNUP=1 → deploy) → submit toll-free verification (TFV) once
  4. TFV approved → TWILIO_FROM + SMS_ENABLED=1 → deploy → board "done" → unload this launchd job
     (TWILIO_FROM waits for approval: an unverified toll-free number cannot deliver)
Secrets come from Keychain (service "getduonow") and the environment and are never printed.
Business details for the verification form come from TFV_* environment variables.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from duo_monitor import messages  # noqa: E402

SITE = os.environ.get("SITE", "https://duo.solescout.ai")
SERVICE = os.environ.get("RENDER_SERVICE_ID", "")
JOB = "com.getduonow.twilio-golive"
TOLL_FREE = ("+1800", "+1833", "+1844", "+1855", "+1866", "+1877", "+1888")
CONTACT = "support@solescout.ai"


def keychain(account: str) -> str:
    return subprocess.check_output(["security", "find-generic-password", "-s", "getduonow", "-a", account, "-w"],
                                   text=True).strip()


def keychain_set(account: str, value: str) -> None:
    subprocess.run(["security", "add-generic-password", "-U", "-s", "getduonow", "-a", account, "-w", value], check=True)


def render_key() -> str:
    return os.environ["RENDER_API_KEY"]


def board(status: str, note: str) -> None:
    if (ROOT / "ops/check.py").exists():      # optional private ops board
        subprocess.run([sys.executable, str(ROOT / "ops/check.py"), "set", "twilio", status, note], check=False)
    print(f"[{status}] {note}", flush=True)


class Twilio:
    def __init__(self) -> None:
        self.sid = keychain("twilio-account-sid")
        self.http = httpx.Client(auth=(self.sid, keychain("twilio-auth-token")), timeout=30)
        self.api = f"https://api.twilio.com/2010-04-01/Accounts/{self.sid}"

    def call(self, method: str, url: str, **kw) -> dict:
        r = self.http.request(method, url, **kw)
        if r.status_code >= 300:
            raise SystemExit(f"twilio {method} {url.split('/')[-1]} → {r.status_code} {r.text[:300]}")
        return r.json()

    def approved_profile(self) -> tuple[str | None, str]:
        rows = self.call("GET", "https://trusthub.twilio.com/v1/CustomerProfiles")["results"]
        ok = [p["sid"] for p in rows if p["status"] == "twilio-approved"]
        return (ok[0] if ok else None), ", ".join(p["status"] for p in rows) or "none"

    def number(self) -> dict:
        """The account's toll-free number, bought on first call; its incoming texts always go to our webhook."""
        hook = {"SmsUrl": f"{SITE}/twilio/inbound", "SmsMethod": "POST"}
        own = [n for n in self.call("GET", f"{self.api}/IncomingPhoneNumbers.json")["incoming_phone_numbers"]
               if n["phone_number"].startswith(TOLL_FREE)]
        if own:
            n = own[0]
            if n["sms_url"] != hook["SmsUrl"]:
                n = self.call("POST", f"{self.api}/IncomingPhoneNumbers/{n['sid']}.json", data=hook)
            return n
        offers = self.call("GET", f"{self.api}/AvailablePhoneNumbers/US/TollFree.json",
                           params={"SmsEnabled": "true", "PageSize": 5})["available_phone_numbers"]
        if not offers:
            raise SystemExit("no toll-free numbers offered")
        n = self.call("POST", f"{self.api}/IncomingPhoneNumbers.json",
                      data={"PhoneNumber": offers[0]["phone_number"], "FriendlyName": "GetDuoNow", **hook})
        keychain_set("twilio-from", n["phone_number"])
        keychain_set("twilio-number-sid", n["sid"])
        print(f"bought {n['phone_number']}", flush=True)
        return n

    def verification(self, number_sid: str, profile_sid: str) -> dict:
        """The number's toll-free verification, submitted once (a rejection is fixed by a person, not resubmitted)."""
        url = "https://messaging.twilio.com/v1/Tollfree/Verifications"
        found = self.call("GET", url, params={"TollfreePhoneNumberSid": number_sid})["verifications"]
        if found:
            return found[0]
        help_text = (f"GetDuoNow: Apple Store pickup restock alerts. Msg & data rates may apply. Help: {CONTACT} "
                     "Reply STOP to opt out.")
        data = [
            ("TollfreePhoneNumberSid", number_sid), ("CustomerProfileSid", profile_sid),
            ("BusinessName", os.environ["TFV_BUSINESS_NAME"]), ("DoingBusinessAs", "GetDuoNow"), ("BusinessWebsite", SITE),
            ("BusinessType", "PRIVATE_PROFIT"), ("BusinessRegistrationNumber", os.environ["TFV_EIN"]),
            ("BusinessRegistrationAuthority", "EIN"), ("BusinessRegistrationCountry", "US"),
            ("BusinessStreetAddress", os.environ["TFV_STREET"]), ("BusinessCity", os.environ["TFV_CITY"]),
            ("BusinessStateProvinceRegion", os.environ["TFV_STATE"]), ("BusinessPostalCode", os.environ["TFV_ZIP"]),
            ("BusinessCountry", "US"),
            ("BusinessContactFirstName", os.environ["TFV_FIRST_NAME"]), ("BusinessContactLastName", os.environ["TFV_LAST_NAME"]),
            ("BusinessContactEmail", CONTACT), ("BusinessContactPhone", os.environ["TFV_PHONE"]),
            ("NotificationEmail", CONTACT), ("ExternalReferenceId", "getduonow"),
            ("UseCaseCategories", "ACCOUNT_NOTIFICATIONS"),
            ("UseCaseSummary", "GetDuoNow sells one-time watch passes on duo.solescout.ai: a customer picks Apple Store "
             "locations and iPhone models, and may tick an unchecked SMS consent box. We text that customer only when a "
             "watched model becomes available for in-store pickup at a chosen store (a few texts per watch at most), "
             "with the Apple order link. Service alerts the customer paid for; no marketing, no lists shared."),
            ("ProductionMessageSample", messages.alert_sms("iPhone Duo 256GB Star White", "Riverside Mall",
                                                           "MK1E4LL/A", "en")),
            ("OptInType", "WEB_FORM"), ("OptInImageUrls", f"{SITE}/static/sms-optin.png"),
            ("OptInConfirmationMessage", messages.welcome_sms("en")), ("HelpMessageSample", help_text),
            ("PrivacyPolicyUrl", f"{SITE}/terms#privacy"), ("TermsAndConditionsUrl", f"{SITE}/terms#sms"),
            ("MessageVolume", "1,000"), ("AgeGatedContent", "false"),
            ("AdditionalInformation", f"Opt-in: an unchecked checkbox with the full SMS disclosure on the signup form at "
             f"{SITE} (screenshot attached); the phone number is kept only when ticked. STOP / HELP handled by Twilio "
             "Advanced Opt-Out and our webhook. Customer support: " + CONTACT),
        ]
        return self.call("POST", url, data=data)


class Render:
    def __init__(self) -> None:
        self.http = httpx.Client(headers={"Authorization": f"Bearer {render_key()}"}, timeout=30)
        self.base = f"https://api.render.com/v1/services/{SERVICE}"

    def ensure_env(self, want: dict[str, str]) -> bool:
        """Set the env vars that differ; True when anything changed (a deploy is then due)."""
        changed = False
        for k, v in want.items():
            r = self.http.get(f"{self.base}/env-vars/{k}")
            if r.status_code == 200 and r.json().get("value") == v:
                continue
            self.http.put(f"{self.base}/env-vars/{k}", json={"value": v}).raise_for_status()
            changed = True
        return changed

    def deploy(self) -> None:
        self.http.post(f"{self.base}/deploys", json={}).raise_for_status()


def live(key: str) -> bool:
    try:
        return httpx.get(f"{SITE}/api/config", timeout=20).json().get(key) is True
    except (httpx.HTTPError, ValueError):
        return False


def main() -> None:
    tw, render = Twilio(), Render()
    profile, states = tw.approved_profile()
    if not profile:
        board("user", f"可选：自有号码要你本人在 Twilio 做身份验证（证件+自拍）；短信现由 Textbelt 发。资料状态: {states}")
        return
    n = tw.number()
    if not live("sms_signup"):                 # TFV reviews the live opt-in form
        if render.ensure_env({"SMS_SIGNUP": "1"}):
            render.deploy()
        board("doing", f"号码 {n['phone_number']} 已买；等网站打开短信勾选框后提交 TFV")
        return
    tfv = tw.verification(n["sid"], profile)
    status = tfv["status"]
    if status == "TWILIO_REJECTED":
        board("blocked", f"TFV 被拒: {tfv.get('rejection_reason') or tfv.get('error_code')} — 要改资料重提")
    elif status != "TWILIO_APPROVED":
        board("doing", f"TFV 审核中（{status}），号码 {n['phone_number']}；批下来自动开短信")
    else:
        if render.ensure_env({"TWILIO_FROM": n["phone_number"], "SMS_ENABLED": "1"}):
            render.deploy()
        board("done", f"短信已切到自有号码 {n['phone_number']}（TFV 已批）")
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{JOB}"], check=False)


if __name__ == "__main__":
    main()
