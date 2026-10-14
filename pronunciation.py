#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Levithan Voice AI - Universal Pronunciation Helper

Features:
- Separate pronunciation dictionary for each novel.
- Detects repeated capitalized name candidates dynamically.
- Supports user corrections without hardcoding characters from one novel.
- Preserves the original TXT file.
- Can normalize common English terms for Hindi TTS.
- Saves novel-specific pronunciation mappings as JSON.

Important:
- Automatic name detection is heuristic, not perfect.
- Romanized Chinese/fantasy names may require a pronunciation correction.
- This module does not generate audio itself.
"""

import json
import re
import hashlib
from pathlib import Path
from collections import Counter


BASE_DIR = Path(__file__).resolve().parent
PRONUNCIATION_DIR = BASE_DIR / "pronunciation_data"

PRONUNCIATION_DIR.mkdir(parents=True, exist_ok=True)


# Only generic terms are included here.
# Character names are NOT hardcoded.
DEFAULT_WORDS = {
    "system": "सिस्टम",
    "level": "लेवल",
    "skill": "स्किल",
    "skills": "स्किल्स",
    "quest": "क्वेस्ट",
    "mission": "मिशन",
    "reward": "रिवॉर्ड",
    "rewards": "रिवॉर्ड्स",
    "evolution": "इवोल्यूशन",
    "spirit": "स्पिरिट",
    "spirits": "स्पिरिट्स",
    "energy": "एनर्जी",
    "power": "पावर",
    "powers": "पावर्स",
    "ability": "एबिलिटी",
    "abilities": "एबिलिटीज",
    "awakening": "अवेकनिंग",
    "cultivation": "कल्टिवेशन",
    "technique": "टेक्नीक",
    "techniques": "टेक्नीक्स",
    "academy": "अकादमी",
    "master": "मास्टर",
    "rank": "रैंक",
    "ranks": "रैंक्स",
    "battle": "बैटल",
    "battlefield": "बैटलफील्ड",
    "monster": "मॉन्स्टर",
    "monsters": "मॉन्स्टर्स",
    "dragon": "ड्रैगन",
    "dragons": "ड्रैगन्स",
    "phoenix": "फीनिक्स",
    "demon": "डेमन",
    "demons": "डेमन्स",
    "immortal": "इम्मॉर्टल",
    "immortality": "इम्मॉर्टैलिटी",
    "sword": "स्वॉर्ड",
    "swords": "स्वॉर्ड्स",
    "magic": "मैजिक",
    "magical": "मैजिकल",
    "kingdom": "किंगडम",
    "emperor": "एम्परर",
    "empress": "एम्प्रेस",
    "princess": "प्रिंसेस",
    "prince": "प्रिंस",
    "captain": "कैप्टन",
    "commander": "कमांडर",
    "interface": "इंटरफेस",
    "inventory": "इन्वेंटरी",
    "upgrade": "अपग्रेड",
    "upgraded": "अपग्रेडेड",
    "upgradeable": "अपग्रेडेबल",
    "notification": "नोटिफिकेशन",
    "notifications": "नोटिफिकेशन्स",
    "experience": "एक्सपीरियंस",
    "points": "पॉइंट्स",
    "point": "पॉइंट",
    "status": "स्टेटस",
    "statistics": "स्टैटिस्टिक्स",
    "strength": "स्ट्रेंथ",
    "agility": "एजिलिटी",
    "intelligence": "इंटेलिजेंस",
    "defense": "डिफेंस",
    "attack": "अटैक",
    "critical": "क्रिटिकल",
    "damage": "डैमेज",
    "cooldown": "कूलडाउन",
    "summon": "समन",
    "summoning": "समनिंग",
    "contract": "कॉन्ट्रैक्ट",
    "contracts": "कॉन्ट्रैक्ट्स",
    "questline": "क्वेस्टलाइन",
    "achievement": "अचीवमेंट",
    "achievements": "अचीवमेंट्स",
    "teleport": "टेलीपोर्ट",
    "teleportation": "टेलीपोर्टेशन",
    "dimension": "डाइमेंशन",
    "dimensions": "डाइमेंशन्स",
    "universe": "यूनिवर्स",
    "multiverse": "मल्टीवर्स",
    "galaxy": "गैलेक्सी",
    "galaxies": "गैलेक्सीज",
    "planet": "प्लैनेट",
    "planets": "प्लैनेट्स",
}


def _slugify(text: str) -> str:
    """
    Generate a stable filename for a novel.
    Different novel titles get separate dictionaries.
    """
    text = text.strip().lower()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    text = text.strip("_")

    if not text:
        text = "untitled_novel"

    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:10]

    return f"{text[:60]}_{digest}"


def _dictionary_path(novel_id: str) -> Path:
    return PRONUNCIATION_DIR / f"{_slugify(novel_id)}.json"


def load_dictionary(novel_id: str) -> dict:
    """
    Load only this novel's corrections.
    Character names are not shared between novels.
    """
    path = _dictionary_path(novel_id)

    if not path.exists():
        return {}

    try:
        data = json.loads(path.read_text(encoding="utf-8"))

        if isinstance(data, dict):
            return {
                str(k): str(v)
                for k, v in data.items()
                if str(k).strip() and str(v).strip()
            }

    except (OSError, json.JSONDecodeError):
        pass

    return {}


def save_dictionary(novel_id: str, mapping: dict) -> None:
    """
    Save corrections for a specific novel.
    """
    path = _dictionary_path(novel_id)

    clean = {}

    for original, pronunciation in mapping.items():
        original = str(original).strip()
        pronunciation = str(pronunciation).strip()

        if original and pronunciation:
            clean[original] = pronunciation

    path.write_text(
        json.dumps(
            clean,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def add_correction(
    novel_id: str,
    original: str,
    pronunciation: str,
) -> None:
    """
    Add or update a correction for one novel.

    Example:
        add_correction("Novel A", "Zhou Fan", "झोउ फैन")
    """
    original = original.strip()
    pronunciation = pronunciation.strip()

    if not original or not pronunciation:
        raise ValueError("Original name and pronunciation are required.")

    mapping = load_dictionary(novel_id)
    mapping[original] = pronunciation

    save_dictionary(novel_id, mapping)


def remove_correction(novel_id: str, original: str) -> bool:
    """
    Remove one saved correction.
    """
    mapping = load_dictionary(novel_id)

    if original not in mapping:
        return False

    del mapping[original]
    save_dictionary(novel_id, mapping)

    return True


def get_corrections(novel_id: str) -> dict:
    """
    Return all saved corrections for a novel.
    """
    return load_dictionary(novel_id)


def detect_name_candidates(text: str, minimum_repetitions: int = 2) -> list:
    """
    Heuristically detect repeated capitalized Roman-script terms.

    This does not guarantee that every candidate is a person's name.
    The output is only a review list; no candidate is automatically
    converted without a saved correction.
    """

    # Avoid treating the first word of every sentence as a name.
    # Candidates are words/phrases with internal capitalization,
    # or repeated multi-word capitalized phrases.
    phrase_pattern = re.compile(
        r"\b(?:[A-Z][a-zA-Z'-]{1,}"
        r"(?:\s+(?:of|the|and|de|van|von|[A-Z][a-zA-Z'-]{1,})){1,3})\b"
    )

    word_pattern = re.compile(
        r"\b[A-Z][a-zA-Z'-]{2,}\b"
    )

    phrases = phrase_pattern.findall(text)
    words = word_pattern.findall(text)

    counter = Counter()

    for phrase in phrases:
        normalized = " ".join(phrase.split())
        counter[normalized] += 1

    for word in words:
        counter[word] += 1

    candidates = []

    for candidate, count in counter.items():
        if count < minimum_repetitions:
            continue

        # Skip common sentence-start words and generic terms.
        if candidate.lower() in {
            "the", "this", "that", "then", "when",
            "what", "where", "there", "after",
            "before", "however", "suddenly",
            "finally", "meanwhile", "because",
            "although", "chapter", "volume",
        }:
            continue

        candidates.append({
            "text": candidate,
            "count": count,
        })

    candidates.sort(
        key=lambda item: (-item["count"], item["text"].lower())
    )

    return candidates


def _build_replacement_pattern(original: str):
    """
    Build a case-insensitive replacement pattern while allowing
    flexible whitespace in multi-word names.
    """
    parts = re.split(r"\s+", original.strip())

    if not parts:
        return None

    pattern = r"(?<!\w)" + r"\s+".join(
        re.escape(part) for part in parts
    ) + r"(?!\w)"

    try:
        return re.compile(pattern, flags=re.IGNORECASE)
    except re.error:
        return None


def apply_pronunciation_corrections(
    text: str,
    novel_id: str,
) -> str:
    """
    Apply novel-specific corrections first, then generic English
    term pronunciations.

    Saved character-name corrections take priority.
    """

    result = text

    custom = load_dictionary(novel_id)

    # Replace longer phrases first so that "Name One" takes priority
    # over a single word such as "Name".
    for original in sorted(
        custom.keys(),
        key=len,
        reverse=True,
    ):
        pattern = _build_replacement_pattern(original)

        if pattern is not None:
            result = pattern.sub(
                lambda match: custom[original],
                result,
            )

    # Replace only standalone generic terms.
    # Do not replace inside larger words.
    for original in sorted(
        DEFAULT_WORDS.keys(),
        key=len,
        reverse=True,
    ):
        pronunciation = DEFAULT_WORDS[original]

        pattern = re.compile(
            r"(?<![A-Za-z])"
            + re.escape(original)
            + r"(?![A-Za-z])",
            flags=re.IGNORECASE,
        )

        result = pattern.sub(
            lambda match: pronunciation,
            result,
        )

    return result


def process_text(
    text: str,
    novel_id: str,
    apply_generic_words: bool = True,
) -> str:
    """
    Main function to call from bot.py.

    - Applies corrections saved for this novel.
    - Optionally converts generic English TTS terms.
    - Does not automatically invent pronunciations for unknown names.
    """

    if not isinstance(text, str):
        raise TypeError("text must be a string")

    if not novel_id or not novel_id.strip():
        raise ValueError("novel_id must be provided")

    result = text

    custom = load_dictionary(novel_id)

    for original in sorted(custom, key=len, reverse=True):
        pattern = _build_replacement_pattern(original)

        if pattern is not None:
            result = pattern.sub(
                lambda match: custom[original],
                result,
            )

    if apply_generic_words:
        for original in sorted(DEFAULT_WORDS, key=len, reverse=True):
            pronunciation = DEFAULT_WORDS[original]

            pattern = re.compile(
                r"(?<![A-Za-z])"
                + re.escape(original)
                + r"(?![A-Za-z])",
                flags=re.IGNORECASE,
            )

            result = pattern.sub(
                lambda match: pronunciation,
                result,
            )

    return result


def write_candidate_report(text: str, output_path: str) -> list:
    """
    Write a candidate-name review file for a novel.
    It lists repeated capitalized terms but does not guess pronunciation.
    """
    candidates = detect_name_candidates(text)

    lines = [
        "# Candidate terms detected automatically",
        "# Check the list; not every item is necessarily a name.",
        "# Format for your corrections:",
        "# Original=Hindi pronunciation",
        "",
    ]

    for item in candidates:
        lines.append(f"{item['text']}  [repeated {item['count']} times]")

    Path(output_path).write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )

    return candidates


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Novel-specific pronunciation correction helper."
    )

    parser.add_argument("--novel", required=True, help="Novel title or ID")
    parser.add_argument("--input", help="Input TXT file")
    parser.add_argument("--output", help="Output TXT file")
    parser.add_argument(
        "--candidates",
        help="Write a candidate-name report to this file",
    )
    parser.add_argument(
        "--add",
        nargs=2,
        metavar=("ORIGINAL", "PRONUNCIATION"),
        help="Save a pronunciation correction for this novel",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="Print saved corrections",
    )

    args = parser.parse_args()

    if args.add:
        add_correction(args.novel, args.add[0], args.add[1])
        print("Correction saved for this novel.")

    if args.list:
        print(json.dumps(
            get_corrections(args.novel),
            ensure_ascii=False,
            indent=2,
        ))

    if args.input:
        source = Path(args.input)
        text = source.read_text(encoding="utf-8-sig")

        if args.candidates:
            candidates = write_candidate_report(
                text,
                args.candidates,
            )
            print(f"Candidate report created: {args.candidates}")
            print(f"Candidates found: {len(candidates)}")

        if args.output:
            corrected = process_text(text, args.novel)
            Path(args.output).write_text(
                corrected,
                encoding="utf-8",
            )
            print(f"Processed TXT created: {args.output}")

    if not any([args.add, args.list, args.input]):
        parser.print_help()
'''
path = "/mnt/data/pronunciation.py"
Path(path).write_text(code, encoding="utf-8")
print(f"Created {path} ({len(code.splitlines())} lines)")
