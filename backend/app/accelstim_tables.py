"""AI-transcribed source tables for one immutable public PDF, NOT an approval.

Source: AccelStim Rev AB 2026, physical PDF pages, SHA256 below. Numeric
clinical values and formulas were checked against rendered original pages.
This is a bounded editorial repair profile, not a generic PDF/OCR algorithm.
"""
import re
import hashlib
from pathlib import Path

SOURCE_SHA256 = "c3fa82d25e335dc9beb39f371b9c47984812936029b00dbe5cb7a4c8ab03bb40"
REVISION = "accelstim-revab-ai-tables-20261019-v1"
PROFILE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def table(headers, rows):
    def cell(value):
        return str(value).replace("|", "\\|").replace("\n", "<br>")
    return "\n".join(["| " + " | ".join(map(cell, headers)) + " |",
                      "| " + " | ".join("---" for _ in headers) + " |"] +
                     ["| " + " | ".join(map(cell, row)) + " |" for row in rows])


def span(text, start, end, replacement):
    """Match layout whitespace, but fail closed if source anchors drift."""
    def pattern(value):
        return r"\s+".join(re.escape(part) for part in value.split())
    starts = list(re.finditer(pattern(start), text))
    if len(starts) != 1:
        raise ValueError(f"PDF repair start anchor ambiguous/missing: {start}")
    left = starts[0].start()
    line_start = text.rfind("\n", 0, left) + 1
    if not text[line_start:left].strip():
        left = line_start  # Do not inherit layout indentation as Markdown code.
    if end is None:
        right = len(text)
    else:
        stops = list(re.finditer(pattern(end), text[starts[0].end():]))
        if len(stops) != 1:
            raise ValueError(f"PDF repair end anchor ambiguous/missing: {end}")
        right = starts[0].end() + stops[0].start()
    return text[:left] + replacement.strip() + "\n\n" + text[right:]


ENVIRONMENT = table(["Condition / Condición", "Transport / Transporte", "Storage / Almacenamiento"], [
    ["Ambient temperature (°C)", "-20 to +60", "5 to 30"],
    ["Relative humidity (non-condensing)", "10% to 90%", "15% to 93%"],
    ["Atmospheric pressure (hPa)", "500 to 1060", "700 to 1060"],
])
POWER = table(["Power supply field / Campo de alimentación", "Value / Valor"], [
    ["Model", "WR9QA1500USBCNAIMR6B"], ["Brand", "GLOBTEK, INC"],
    ["Input voltage", "100 - 240 VAC"], ["Input frequency", "50 - 60 Hz"],
    ["Max. input current", "0.6 A"], ["Output voltage", "5 VDC"],
    ["Max. output current", "1.5 A"], ["Short-circuit protection", "Auto-restart"],
    ["Insulation class", "II"],
]) + "\n\nManufacturer's note: Orthofix may provide different power supply models tested and approved according to IEC60601-1 or IEC62368-2. Use only the power supply provided."

RF_DISTANCE = table(["Maximum transmitter output power P (W)", "150 kHz to 80 MHz: d=1.2*sqrt(P) (m)",
                     "80 MHz to 800 MHz: d=1.2*sqrt(P) (m)", "800 MHz to 2.5 GHz: d=2.3*sqrt(P) (m)"], [
    ["0.01", "0.12", "0.12", "0.23"], ["0.1", "0.38", "0.38", "0.73"],
    ["1", "1.2", "1.2", "2.3"], ["10", "3.8", "3.8", "7.3"], ["100", "12", "12", "23"],
])
FORMULA_DIFFERENCE = ("AI transcription note / Nota de transcripción IA: sqrt(P) denotes the square root of P, not P. "
    "The printed adjacent pages are not fully consistent: physical pages 26/60 show d=2.3*sqrt(P) beside "
    "80-800 MHz and 800 MHz-2.7 GHz; physical pages 27/61 show coefficients 1.2, 1.2, 2.3 and an upper band of 2.5 GHz. "
    "The source discrepancy is preserved, not resolved. Do not derive a clinical/device placement recommendation from this transcription; consult the original pages and manufacturer.")

EMISSIONS = table(["Emissions test", "Compliance", "Electromagnetic environment - guidance"], [
    ["RF emissions, CISPR 11", "Group 1", "RF energy is used only for the internal function; emissions are very low and not likely to interfere with nearby electronic equipment."],
    ["RF emissions, CISPR 11", "Class B", "Suitable for all establishments, including domestic establishments and those directly connected to the public low-voltage power supply network supplying buildings used for domestic purposes. This guidance cell spans this row and the following two N/A rows in the original."],
    ["N/A (unlabelled test row in original)", "N/A", "Shared Class B environment-guidance cell above; no test name inferred."],
    ["N/A (unlabelled test row in original)", "N/A", "Shared Class B environment-guidance cell above; no test name inferred."],
])
ESD = ["Electrostatic discharge (ESD), IEC 61000-4-2", "±8 kV contact; ±2, ±4, ±8, ±15 kV air",
       "±8 kV contact; ±2, ±4, ±8, ±15 kV air", "Floors should be wood, concrete or ceramic tile. If covered with synthetic material, relative humidity should be at least 30%."]
BURST = ["Electrical fast transient/burst, IEC 61000-4-4", "±2 kV, 100 kHz repetition frequency",
         "±2 kV, 100 kHz repetition frequency", "Mains power quality should be that of a typical commercial or hospital environment."]
SURGE = ["Surge, IEC 61000-4-5", "±0.5, ±1 kV line to line; ±0.5, ±1, ±2 kV line to ground",
         "±0.5, ±1 kV line to line; ±0.5, ±1, ±2 kV line to ground", "Mains power quality should be that of a typical commercial or hospital environment."]
DIPS = ["Voltage dips, short interruptions and voltage variations, IEC 61000-4-11",
        "0% UT, 0.5 cycle at 0°,45°,90°,135°,180°,225°,270°,315°; 0% UT, 1 cycle and 70% UT, 30 cycles, single phase at 0°; 0% UT, 300 cycles",
        "0% UT, 0.5 cycle at 0°,45°,90°,135°,180°,225°,270°,315°; 0% UT, 1 cycle and 70% UT, 30 cycles, single phase at 0°; 0% UT, 300 cycles",
        "Mains power quality should be typical commercial/hospital. If continued operation during mains interruptions is required, the medical electrical equipment/system is recommended to be powered from an uninterruptible supply or battery."]


def immunity(rows):
    return table(["Immunity test", "IEC 60601 test level", "Compliance level", "Environment - guidance"], rows)


def near_field():
    pairs = [(385, 27), (450, 28), (710, 9), (745, 9), (780, 9), (810, 28), (870, 28), (930, 28),
             (1720, 28), (1845, 28), (1970, 28), (2450, 28), (5240, 9), (5500, 9), (5785, 9)]
    return ("\n\n### RF wireless proximity fields - IEC 60601-1-2 clause 8.10\n" +
        table(["Frequency (MHz)", "IEC test level (V/m)", "Compliance level (V/m)"],
              [[hz, level, level] for hz, level in pairs]) +
        "\n\nThe listed frequencies represent RF communications equipment in use at publication of the collateral standard. UT is the AC mains voltage before application of the test level.")


RF_IMMUNITY = immunity([
    ["Conducted RF, IEC 61000-4-6", "3 V, 0.15-80 MHz; 6 V in ISM/amateur bands 0.15-80 MHz; 80% AM at 1 kHz",
     "3 V, 0.15-80 MHz; 6 V in ISM/amateur bands 0.15-80 MHz; 80% AM at 1 kHz",
     "Portable/mobile RF equipment including cables should maintain the separation distance calculated using the applicable frequency equation."],
    ["Radiated RF, IEC 61000-4-3", "10 V/m, 80 MHz-2.7 GHz; 80% AM at 1 kHz",
     "10 V/m, 80 MHz-2.7 GHz; 80% AM at 1 kHz",
     "Printed formula: d=2.3*sqrt(P), shown beside 80 MHz-800 MHz and 800 MHz-2.7 GHz; preserve original discrepancy with the next page."],
]) + "\n\n" + FORMULA_DIFFERENCE

# All values below are complete-case study counts, NOT an individual prognosis.
# A p-value belongs to the whole labelled category, not each individual row.
CLINICAL = {
    31: [
        ("1 Gender", "0.19", [("Female",30,28,2,"93%"),("Male",44,36,8,"82%")]),
        ("2 Age", "0.52", [("<17",1,1,0,"100%"),("18-29",12,9,3,"75%"),("30-49",32,27,5,"84%"),("50-64",21,19,2,"91%"),(">65",8,8,0,"100%")]),
        ("3 Weight", "0.65", [("<65 kg",12,11,1,"92%"),("65-80 kg",35,31,4,"89%"),(">80 kg",27,22,5,"81%")]),
        ("4 Fracture age", "0.001", [("256-356 days",20,19,1,"95%"),("366-730 days",27,24,3,"89%"),("731-1826 days",17,16,1,"94%"),(">1827 days",10,5,5,"50%")]),
        ("5 Total surgical procedures including subsequent interventions", "0.16", [("0",20,15,5,"75%"),("1",15,12,3,"80%"),("2",24,23,1,"96%"),("3 or more",15,14,1,"93%")]),
        ("6 Prior days without surgery (last surgery to SAFHS start)", "0.03", [("<82 days",9,9,0,"100%"),("83-365 days",39,34,5,"87%"),("366-730 days",12,12,0,"100%"),(">731 days",14,9,5,"64%")]),
    ],
    32: [
        ("7 Bone", "0.03", [("Tibia/Tibia-Fibula/Fibula",28,26,2,"93%"),("Femur",13,12,1,"92%"),("Radius/Radius-Ulna/Ulna",7,6,1,"86%"),("Humerus",6,5,1,"83%"),("Metatarsal",4,4,0,"100%"),("Other foot bones (calcaneus)",1,1,0,"100%"),("Ankle (tibio-talar arthrodesis)",2,1,1,"50%"),("Scaphoid",6,2,4,"33%"),("Other hand bones (metacarpal)",1,1,0,"100%"),("Other (4 clavicle, 1 pelvis, 1 rib)",6,6,0,"100%")]),
        ("8 Long bone vs other bones", "0.02", [("Long bones: 28 tibia,13 femur,7 radius,6 humerus,4 metatarsal,1 metacarpal",59,54,5,"92%"),("Other bones:1 calcaneus,4 clavicle,1 pelvis,1 rib,6 scaphoid,2 ankle",15,10,5,"67%")]),
        ("9 Displaced at start of SAFHS therapy", "1.00", [("Missing","(5)","(2)","(3)","not reported"),("No",56,50,6,"89%"),("Yes",13,12,1,"92%")]),
        ("10 Long bone type (long bone cases only)", "0.05", [("Missing","(5)","(3)","(2)","not reported"),("Metaphyseal",8,6,2,"75%"),("Diaphyseal",46,45,1,"98%")]),
    ],
    33: [
        ("11 Initial fracture type", "0.16", [("Missing","(4)","(2)","(2)","not reported"),("Closed",40,34,6,"85%"),("Open",22,21,1,"95%"),("Arthrodesis",2,1,1,"50%"),("Osteotomy",6,6,0,"100%")]),
        ("12 Fixation: IM rod (long bone cases N=59)", "0.31", [("No",43,38,5,"88%"),("Yes",16,16,0,"100%")]),
        ("12 Fixation: open reduction/internal fixation (ORIF)", "1.00", [("No",51,44,7,"86%"),("Yes",24,21,3,"88%")]),
        ("12 Fixation: external (long bone cases N=59)", "0.58", [("No",50,46,4,"92%"),("Yes",9,8,1,"89%")]),
        ("12 Fixation: conservative (cast/splint/brace)", "0.44", [("No",59,52,7,"88%"),("Yes",16,13,3,"81%")]),
        ("12 Fixation: IM rod/ORIF/external/conservative", "0.16", [("No",11,8,3,"73%"),("Yes",64,57,7,"89%")]),
        ("13 Prior failed lithotripsy", "1.00", [("No",73,63,10,"86%"),("Yes",2,2,0,"100%")]),
        ("14 Smoking status", "0.47", [("Missing","(2)","(2)","(0)","not reported"),("Never smoked",34,31,3,"91%"),("Stopped before SAFHS start",10,8,2,"80%"),("Smoke at SAFHS start",28,23,5,"82%")]),
        ("15 Nonunion type", "0.57", [("Missing","(22)","(17)","(5)","not reported"),("Atrophic",41,36,5,"88%"),("Hypertrophic",11,11,0,"100%")]),
    ],
}


def clinical(page):
    canonical = page - 34 if page >= 65 else page
    blocks = ["## Table 1 / Tabla 1: Efficacy results for SAFHS treated completed cases",
        "AI transcription: canonical English field labels for the original table; not an official translation. These are study results, not patient-specific predictions. Parenthesised missing counts remain parenthesised; no missing percentages are calculated."]
    for name, pvalue, rows in CLINICAL[canonical]:
        blocks += [f"### {name}\nGroup-level p-value: {pvalue} (shared cell; not a row-specific p-value).",
                   table(["Category", "Total", "Healed", "Failed", "% Healed"], rows)]
    if canonical == 31:
        blocks.append("*Two-sided exact p-value, Fisher's exact test, testing homogeneity of strata.")
    return "\n\n".join(blocks)


def battery():
    return ("## Visual and audio battery indicators / Indicadores de batería\n"
        "Alarm conditions may interrupt operation. Contact Patient Care for further technical assistance.\n\n" +
        table(["Displayed state (textual description, not image recognition)", "Sound / indication", "Meaning / action"], [
            ["Battery indicator at upper-right", "Notches decrease as battery runs down", "When fully charged the device can provide up to five treatments."],
            ["Red low-battery icon", "Three short beeps; icon blinks three times until connected to supply", "Connect supplied power supply to recharge."],
            ["Blinking lightning icon", "Appears when supply connected", "Charging has started. If faulty supply prevents icon appearing, contact Patient Care."],
            ["Full battery with checkmark", "Charging completed", "Charging completed; not a fault or expired-life state."],
            ["E-prefixed eight-digit number", "Four medium beeps; power down after 12 seconds", "Refer to charging section and contact Patient Care."],
        ]) + "\n\nPatient Care: 800.535.4492. Original icons are not available for image recognition in this text-only repair.")


def faults():
    return ("## Troubleshooting / Solución de problemas\n"
        "Alarm conditions interrupt treatment or charging. The original has three distinct rows:\n\n" +
        table(["Original display/state", "Audio", "Problem and solution (bound to this row only)"], [
            ["E315 / Expired Device Life", "Two long beeps", "Daily treatments for up to 365 days from first use; E315 shown for five minutes or until turned off. The original does NOT assign the gel/restart procedure from the next row to this state."],
            ["E3XX / red X / Fault Detected", "Two long beeps", "Treatment stops. Check gel on the transducer, then hold stand-by to power off and press stand-by to restart. If message remains after checking gel, turn off and contact Patient Care 800.535.4492."],
            ["Charger Not Supported", "No audio value specified in this row", "Only use the Orthofix-supplied charger; an alternative charger can permanently damage the device. Refer to Recharging the device Battery section."],
        ]) + "\n\nAI transcription: do not transfer an action from one state to a different state; this is not a diagnosis or complete image interpretation.")


def repair_page(source_sha, page_number, raw):
    """Return repaired text or None. No changes whatsoever for a different PDF."""
    if source_sha != SOURCE_SHA256:
        return None
    n, text = page_number, raw
    if n in (31,32,33,65,66,67):
        anchor = "Table 1:" if n < 65 else "Tabla 1:"
        if anchor not in raw:
            raise ValueError("Missing clinical table title")
        text = clinical(n)
    elif n in (18,52):
        text = span(raw, "Transport Storage" if n == 18 else "Transporte Almacenamiento",
                    "When new," if n == 18 else "Cuando es nueva,", ENVIRONMENT)
    elif n in (21,55):
        text = span(raw, "External power supply used as battery recharger" if n == 21 else "Fuente de alimentación externa utilizada como cargador de batería",
                    "OPERATING SPECIFICATION" if n == 21 else "ESPECIFICACIÓN DE FUNCIONAMIENTO", POWER)
    elif n in (16,50):
        text = span(raw, "VISUAL AND AUDIO BATTERY INDICATORS" if n == 16 else "INDICADORES VISUALES Y AUDIBLES DE LA BATERÍA", None, battery())
    elif n in (17,51):
        anchor = "Troubleshooting" if n == 17 else "Solución de problemas"
        text = span(raw, anchor, None, faults())
    elif n in (26,60):
        text = span(raw, "Immunity Test" if n == 26 else "Prueba de", "NOTE 1" if n == 26 else "NOTA 1", RF_IMMUNITY)
    elif n in (27,61):
        text = span(raw, "Rated Maximum" if n == 27 else "Potencia de salida",
                    "For transmitters" if n == 27 else "Para los transmisores", RF_DISTANCE + "\n\n" + FORMULA_DIFFERENCE)
    elif n == 24:
        text = span(raw, "Emissions Test", "Guidance and Manufacturer’s Declaration – Electromagnetic Immunity", EMISSIONS)
        text = span(text, "Immunity Test", None, immunity([ESD,BURST]))
    elif n == 58:
        text = span(raw, "Prueba de emisiones", "Orientación y declaración del fabricante: inmunidad electromagnética", EMISSIONS)
        text = span(text, "Prueba de", None, immunity([ESD]))
    elif n in (25,59):
        if "IEC 61000-4-5" not in raw:
            raise ValueError("Missing surge source table")
        text = "## Electromagnetic immunity / Inmunidad electromagnética\n\n" + immunity(
            ([BURST] if n == 59 else []) + [SURGE,DIPS]) + near_field()
    else:
        return None
    return ("AI-assisted table/formula transcription; source-specific, pending independent confirmation. "
            "Canonical table fields are in English, including on Spanish source pages; not an official translation.\n\n" + text)


REPAIRED_PAGES = {16,17,18,21,24,25,26,27,31,32,33,50,51,52,55,58,59,60,61,65,66,67}
