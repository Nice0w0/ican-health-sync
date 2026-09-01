#!/usr/bin/env python3
"""
Build the one-tap share-sheet shortcut that talks to the conversion API.

Nine actions, all Apple's own -- no a-Shell, so nothing interrupts the run and
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


def out(action_uuid, name="output"):
    return {"Value": {"OutputName": name, "OutputUUID": action_uuid,
                      "Type": "ActionOutput"},
            "WFSerializationType": "WFTextTokenAttachment"}


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


def build(url, token, unit="mg/dL"):
    find_u, since_u, http_u, dict_u = u(), u(), u(), u()
    value_u, datetext_u, date_u = u(), u(), u()
    group = u()

    # The unit is pinned in both places at once: the API converts whatever the
    # export declares into `unit`, and Log Health Sample below is set to the
    # same one. They cannot drift apart, because Shortcuts' unit picker cannot
    # take a variable and a mismatch would record a dangerously wrong number.
    base = "%s?token=%s&unit=%s&since=" % (url, token, quote(unit))

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
                        }],
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
            # Turn that sample into a Date the URL can carry.
            "WFWorkflowActionIdentifier": DETECT_DATE,
            "WFWorkflowActionParameters": {
                "UUID": since_u,
                "WFInput": out(find_u, "Health Samples"),
            },
        },
        {
            # The shared .xls goes up as the raw body; the API accepts that or
            # multipart. Token and cursor ride in the query string so no header
            # dictionary has to be configured.
            "WFWorkflowActionIdentifier": GET_URL,
            "WFWorkflowActionParameters": {
                "UUID": http_u,
                "WFURL": text_with(since_u, prefix=base, name="Date"),
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
    ap.add_argument("-o", "--output", default="iCan to Health.shortcut")
    args = ap.parse_args()
    with open(args.output, "wb") as fh:
        plistlib.dump(build(args.url, args.token, args.unit), fh,
                      fmt=plistlib.FMT_BINARY)
    print("wrote %s" % args.output)


if __name__ == "__main__":
    main()
