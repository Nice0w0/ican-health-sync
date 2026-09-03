#!/usr/bin/env python3
"""
Build the one-tap share-sheet shortcut that talks to the conversion API.

Ten actions, all Apple's own -- no a-Shell, so nothing interrupts the run and
a share really is one tap. `Execute Command` could never be used here: it opens
a-Shell and Shortcuts never resumes afterwards.

Every identifier and parameter shape below was read off a shortcut the user
built on their own device (see sample-*.plist), not inferred, with one
exception noted at the sort/limit keys.

    python3 build_api_shortcut.py --url https://host/api/convert --token XXX
"""

import argparse
import plistlib
import uuid
from urllib.parse import quote

FIND_HEALTH = "is.workflow.actions.filter.health.quantity"
DETECT_DATE = "is.workflow.actions.detect.date"
FORMAT_DATE = "is.workflow.actions.format.date"
GET_URL = "is.workflow.actions.downloadurl"
DETECT_DICT = "is.workflow.actions.detect.dictionary"
REPEAT_EACH = "is.workflow.actions.repeat.each"
GET_VALUE = "is.workflow.actions.getvalueforkey"
LOG_HEALTH = "is.workflow.actions.health.quantity.log"

CLIENT_VERSION = "5037.0.17"
SAMPLE_TYPE = "Blood Glucose"
OBJ = "￼"  # marks where a variable sits inside a text field


def u():
    return str(uuid.uuid4()).upper()


def out(action_uuid, name="output", prop=None):
    """
    A reference to an earlier action's output.

    `prop` reads one property off that output instead of the whole thing --
    what the app does when you tap a variable token and pick a detail from the
    list underneath it. A Health Sample rendered as text is just its value, so
    without this the date is silently absent: the cursor arrives empty, the
    server sees no `since`, and every share re-imports the entire export.
    """
    value = {"OutputName": name, "OutputUUID": action_uuid,
             "Type": "ActionOutput"}
    if prop:
        value["Aggrandizements"] = [{
            "Type": "WFPropertyVariableAggrandizement",
            "PropertyName": prop,
        }]
    return {"Value": value, "WFSerializationType": "WFTextTokenAttachment"}


def var(name):
    return {"Value": {"Type": "Variable", "VariableName": name},
            "WFSerializationType": "WFTextTokenAttachment"}


def text_with(action_uuid, prefix="", suffix="", name="output"):
    """A text field made of literal text with one action's output embedded."""
    return {
        "Value": {
            "string": prefix + OBJ + suffix,
            "attachmentsByRange": {
                "{%d, 1}" % len(prefix): {
                    "OutputName": name, "OutputUUID": action_uuid,
                    "Type": "ActionOutput", "Aggrandizements": [],
                }
            },
        },
        "WFSerializationType": "WFTextTokenString",
    }


def build(url, token, unit="mg/dL", every=0, window=7):
    find_u, since_u, iso_u, http_u, dict_u = u(), u(), u(), u(), u()
    value_u, datetext_u, date_u = u(), u(), u()
    group = u()

    # The unit is pinned in both places at once: the API converts whatever the
    # export declares into `unit`, and Log Health Sample below is set to the
    # same one. They cannot drift apart, because Shortcuts' unit picker cannot
    # take a variable and a mismatch would record a dangerously wrong number.
    # `every` thins the readings server-side. It is the one setting that
    # decides how long a run takes: the loop below costs four on-device actions
    # per reading, and a raw CGM feed is one reading every three minutes --
    # ~480 a day. At 15 minutes that is 96, and the curve still looks the same
    # in Health.
    thin = ("&every=%d" % every) if every else ""

    # Bound the Health search to a recent window. Without this, action 1 scans
    # every Blood Glucose sample Health has ever held, so the shortcut gets
    # slower with each import even when only one reading is new -- which is the
    # opposite of what the `since` cursor is for. The shape below (Operator
    # 1001, Unit 16) is read off a Find Health Samples action built on device,
    # not inferred.
    date_bound = [{
        "Bounded": True,
        "Operator": 1001,                   # "is in the last ..."
        "Property": "Start Date",
        "Removable": False,
        "Values": {"Number": str(window), "Unit": 16},
    }] if window else []
    base = "%s?token=%s&unit=%s%s&since=" % (url, token, quote(unit), thin)

    actions = [
        {
            # Ask Health what it already has. Its newest Blood Glucose sample
            # is the import cursor -- that is what keeps this stateless: the
            # server never has to remember anything about anyone.
            "WFWorkflowActionIdentifier": FIND_HEALTH,
            "WFWorkflowActionParameters": {
                "UUID": find_u,
                "WFContentItemFilter": {
                    "Value": {
                        "WFActionParameterFilterPrefix": 1,
                        "WFActionParameterFilterTemplates": [{
                            "Bounded": True,
                            "Operator": 4,          # "is"
                            "Property": "Type",
                            "Removable": False,
                            "Values": {"Enumeration": {
                                "Value": SAMPLE_TYPE,
                                "WFSerializationType": "WFStringSubstitutableState"}},
                        }] + date_bound,
                        "WFContentPredicateBoundedDate": False,
                    },
                    "WFSerializationType": "WFContentPredicateTableTemplate",
                },
                # These four are the generic filter-action keys, the one part
                # here not read off a device sample. If the built shortcut does
                # not show "Sort by Start Date, Latest First, Limit 1", set it
                # by hand -- a wrong cursor means duplicates in Health.
                "WFContentItemSortProperty": "Start Date",
                "WFContentItemSortOrder": "Latest First",
                "WFContentItemLimitEnabled": True,
                "WFContentItemLimitNumber": 1,
            },
        },
        {
            # Turn that sample's *Start Date* into a Date the URL can carry.
            "WFWorkflowActionIdentifier": DETECT_DATE,
            "WFWorkflowActionParameters": {
                "UUID": since_u,
                "WFInput": out(find_u, "Health Samples", prop="Start Date"),
            },
        },
        {
            # Spell the cursor as ISO 8601 before it goes anywhere near a URL.
            # Shortcuts renders a Date using the phone's locale and does not
            # escape what it inserts, so a Thai phone produced
            # "3 ก.ย. 2569 21:58" and the space ended the HTTP request line --
            # the server received since=3, read it as a unix timestamp, and
            # cheerfully returned the entire export. Every time.
            # ISO 8601 has no spaces and is always Gregorian, so neither the
            # locale nor the calendar can break it.
            #
            # WFDate must be set explicitly. This action does NOT chain from the
            # previous one the way Log Health Sample does -- leaving it out
            # produced an empty result and, once again, an empty cursor. The key
            # name and its WFTextTokenString shape are read off the user's
            # device after wiring it by hand; both were guessed wrong first.
            "WFWorkflowActionIdentifier": FORMAT_DATE,
            "WFWorkflowActionParameters": {
                "UUID": iso_u,
                "WFDate": text_with(since_u, name="Date"),
                "WFDateFormatStyle": "ISO 8601",
                "WFISO8601IncludeTime": True,
            },
        },
        {
            # The shared .xls goes up as the raw body; the API accepts that or
            # multipart. Token and cursor ride in the query string so no header
            # dictionary has to be configured.
            "WFWorkflowActionIdentifier": GET_URL,
            "WFWorkflowActionParameters": {
                "UUID": http_u,
                "WFURL": text_with(iso_u, prefix=base, name="Formatted Date"),
                "WFHTTPMethod": "POST",
                "WFHTTPBodyType": "File",
                "WFRequestVariable": {
                    "Value": {"Type": "ExtensionInput"},
                    "WFSerializationType": "WFTextTokenAttachment",
                },
            },
        },
        {
            "WFWorkflowActionIdentifier": DETECT_DICT,
            "WFWorkflowActionParameters": {
                "UUID": dict_u, "WFInput": out(http_u, "Contents of URL")},
        },
        {
            "WFWorkflowActionIdentifier": REPEAT_EACH,
            "WFWorkflowActionParameters": {
                "GroupingIdentifier": group, "WFControlFlowMode": 0,
                "WFInput": out(dict_u, "Dictionary")},
        },
        {
            "WFWorkflowActionIdentifier": GET_VALUE,
            "WFWorkflowActionParameters": {
                "UUID": value_u, "WFInput": var("Repeat Item"),
                "WFDictionaryKey": "value", "WFGetDictionaryValueType": "Value"},
        },
        {
            "WFWorkflowActionIdentifier": GET_VALUE,
            "WFWorkflowActionParameters": {
                "UUID": datetext_u, "WFInput": var("Repeat Item"),
                "WFDictionaryKey": "date_text", "WFGetDictionaryValueType": "Value"},
        },
        {
            # date_text is shaped "Sep 01, 2026 at 07:46 PM" precisely because
            # this detector parses it -- confirmed on a Thai-locale phone.
            "WFWorkflowActionIdentifier": DETECT_DATE,
            "WFWorkflowActionParameters": {
                "UUID": date_u, "WFInput": out(datetext_u, "Dictionary Value")},
        },
        {
            "WFWorkflowActionIdentifier": LOG_HEALTH,
            "WFWorkflowActionParameters": {
                "UUID": u(),
                "WFQuantitySampleType": SAMPLE_TYPE,
                # Magnitude takes a BARE reference dict -- no wrapper. The one
                # field in the whole shortcut that does.
                "WFQuantitySampleQuantity": {
                    "Value": {"Magnitude": {"OutputName": "Dictionary Value",
                                            "OutputUUID": value_u,
                                            "Type": "ActionOutput"},
                              "Unit": unit},
                    "WFSerializationType": "WFQuantityFieldValue"},
                "WFQuantitySampleAdditionalQuantity": {
                    "Value": {"Unit": unit},
                    "WFSerializationType": "WFQuantityFieldValue"},
                # The date, unlike Magnitude, IS a token string.
                "WFQuantitySampleDate": text_with(date_u, name="Date"),
            },
        },
        {
            "WFWorkflowActionIdentifier": REPEAT_EACH,
            "WFWorkflowActionParameters": {
                "GroupingIdentifier": group, "UUID": u(), "WFControlFlowMode": 2},
        },
    ]

    return {
        "WFWorkflowActions": actions,
        "WFWorkflowClientVersion": CLIENT_VERSION,
        "WFWorkflowMinimumClientVersion": 900,
        "WFWorkflowMinimumClientVersionString": "900",
        "WFWorkflowHasOutputFallback": False,
        "WFWorkflowHasShortcutInputVariables": True,
        "WFWorkflowIcon": {"WFWorkflowIconGlyphNumber": 61440,
                           "WFWorkflowIconStartColor": -314141441},
        "WFWorkflowImportQuestions": [],
        "WFQuickActionSurfaces": [],
        "WFWorkflowInputContentItemClasses": ["WFGenericFileContentItem"],
        "WFWorkflowOutputContentItemClasses": [],
        "WFWorkflowTypes": ["ActionExtension"],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--token", required=True)
    ap.add_argument("--unit", default="mg/dL", choices=["mg/dL", "mmol/L"],
                    help="must match the unit your Health app expects")
    ap.add_argument("--every", type=int, default=0, metavar="MINUTES",
                    help="thin readings to one per MINUTES; the default 0 "
                         "keeps every reading the CGM recorded")
    ap.add_argument("--window", type=int, default=7, metavar="DAYS",
                    help="how far back action 1 looks for the import cursor; "
                         "0 searches all of Health, which gets slower forever")
    ap.add_argument("-o", "--output", default="iCan to Health.shortcut")
    args = ap.parse_args()
    with open(args.output, "wb") as fh:
        plistlib.dump(build(args.url, args.token, args.unit, args.every,
                            args.window), fh,
                      fmt=plistlib.FMT_BINARY)
    print("wrote %s" % args.output)


if __name__ == "__main__":
    main()
