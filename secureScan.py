import json
import os
import subprocess
from typing import Literal
from typing_extensions import TypedDict

from dotenv import load_dotenv
from groq import Groq
from IPython.display import Image, display
from langgraph.graph import END, START, StateGraph

load_dotenv()
client = Groq(api_key=os.getenv("GROQ_API_KEY"))
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")


# --- State Definition ---

class State(TypedDict):
    contract_code: str
    graph_info: str
    analysis_results: str
    structured_findings: list[dict]
    is_vulnerable: bool
    analysis_success: bool
    llm_explanation: str
    corrected_contract: str
    correction_notes: str


# --- Helper Functions ---

def run_slither(contract_code: str) -> dict:
    contract_filename = "temp_contract.sol"
    report_filename = "slither_report.json"

    try:
        with open(contract_filename, "w", encoding="utf-8") as file:
            file.write(contract_code)

        result = subprocess.run(
            ["slither", contract_filename, "--json", report_filename],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=os.getcwd(),
        )

        if not os.path.exists(report_filename):
            return {
                "success": False,
                "findings": result.stderr or result.stdout or "Slither did not generate a report.",
                "is_vulnerable": False,
            }

        with open(report_filename, "r", encoding="utf-8") as file:
            report = json.load(file)

        detectors = report.get("results", {}).get("detectors", [])
        return {
            "success": True,
            "findings": json.dumps(report, indent=2),
            "is_vulnerable": len(detectors) > 0,
        }

    except subprocess.TimeoutExpired:
        return {"success": False, "findings": "Slither timed out.", "is_vulnerable": False}
    except Exception as err:
        return {"success": False, "findings": str(err), "is_vulnerable": False}
    finally:
        for fname in (contract_filename, report_filename):
            if os.path.exists(fname):
                os.remove(fname)


def extract_vulnerability_findings(slither_output: str) -> list[dict]:
    report = json.loads(slither_output)
    detectors = report.get("results", {}).get("detectors", [])
    findings = []

    for item in detectors:
        locations = [
            {
                "element": el.get("name", "Unknown"),
                "type": el.get("type", "Unknown"),
                "lines": el.get("source_mapping", {}).get("lines", []),
            }
            for el in item.get("elements", [])
        ]

        findings.append({
            "check": item.get("check", "Unknown"),
            "severity": item.get("impact", "Unknown"),
            "confidence": item.get("confidence", "Unknown"),
            "description": item.get("description", ""),
            "locations": locations,
        })

    return findings


# --- LLM Functions ---

def explain_vulnerabilities(findings: list[dict]) -> str:
    if not findings:
        return "No detector findings reported."

    system_prompt = (
        "You are a smart contract security auditor. Analyze the Slither findings.\n"
        "For each issue, state: name, severity, short explanation, impact, and fix.\n"
        "Do not invent issues or assume warnings are automatically critical."
    )

    response = client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(findings, indent=2)},
        ],
        temperature=0.2,
    )
    return response.choices[0].message.content


def generate_corrected_contract(contract_code: str, findings: list[dict], explanation: str) -> str:
    system_prompt = (
        "You are a Solidity security engineer. Fix the vulnerabilities reported.\n"
        "Preserve original functionality, change only what is required, and annotate fixes.\n"
        "Return the full corrected contract followed by a brief summary of changes."
    )

    user_prompt = (
        f"ORIGINAL CONTRACT:\n{contract_code}\n\n"
        f"FINDINGS:\n{json.dumps(findings, indent=2)}\n\n"
        f"AUDIT NOTES:\n{explanation}"
    )

    response = client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.1,
    )
    return response.choices[0].message.content


# --- Graph Nodes & Router ---

def user_input_contract(state: State) -> dict:
    return {
        "contract_code": state["contract_code"],
        "graph_info": "Contract loaded.",
    }


def analyze_contract(state: State) -> dict:
    analysis = run_slither(state["contract_code"])

    if not analysis["success"]:
        return {
            "analysis_results": analysis["findings"],
            "structured_findings": [],
            "analysis_success": False,
            "is_vulnerable": False,
            "graph_info": state["graph_info"] + " Slither run failed.",
        }

    findings = extract_vulnerability_findings(analysis["findings"])
    return {
        "analysis_results": analysis["findings"],
        "structured_findings": findings,
        "analysis_success": True,
        "is_vulnerable": len(findings) > 0,
        "graph_info": state["graph_info"] + " Slither analysis completed.",
    }


def route_analysis(state: State) -> Literal["analysis_failed", "vulnerable_contract", "non_vulnerable_contract"]:
    if not state.get("analysis_success", False):
        return "analysis_failed"
    if state.get("is_vulnerable", False):
        return "vulnerable_contract"
    return "non_vulnerable_contract"


def vulnerable_contract(state: State) -> dict:
    return {"graph_info": state["graph_info"] + " Vulnerabilities flagged."}


def non_vulnerable_contract(state: State) -> dict:
    return {"graph_info": state["graph_info"] + " Clean scan (no Slither findings)."}


def analysis_failed(state: State) -> dict:
    return {"graph_info": state["graph_info"] + " Execution error."}


def explain_findings_node(state: State) -> dict:
    text = explain_vulnerabilities(state["structured_findings"])
    return {
        "llm_explanation": text,
        "graph_info": state["graph_info"] + " Explanation generated.",
    }


def correct_contract(state: State) -> dict:
    code = generate_corrected_contract(
        state["contract_code"],
        state["structured_findings"],
        state["llm_explanation"],
    )
    return {
        "corrected_contract": code,
        "correction_notes": "Generated by Groq.",
        "graph_info": state["graph_info"] + " Correction drafted.",
    }


# --- Graph Construction ---

builder = StateGraph(State)

builder.add_node("user_input", user_input_contract)
builder.add_node("analyze_contract", analyze_contract)
builder.add_node("vulnerable_contract", vulnerable_contract)
builder.add_node("non_vulnerable_contract", non_vulnerable_contract)
builder.add_node("analysis_failed", analysis_failed)
builder.add_node("explain_findings", explain_findings_node)
builder.add_node("correct_contract", correct_contract)

builder.add_edge(START, "user_input")
builder.add_edge("user_input", "analyze_contract")
builder.add_conditional_edges("analyze_contract", route_analysis)

# Vulnerability remediation branch
builder.add_edge("vulnerable_contract", "explain_findings")
builder.add_edge("explain_findings", "correct_contract")
builder.add_edge("correct_contract", END)

# Exit paths
builder.add_edge("non_vulnerable_contract", END)
builder.add_edge("analysis_failed", END)

app = builder.compile()


# --- Execution Example ---

if __name__ == "__main__":
    sample_code = """// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

contract SimpleStorage {
    uint256 public value;

    function setValue(uint256 newValue) public {
        value = newValue;
    }
}"""

    initial_state: State = {
        "contract_code": sample_code,
        "graph_info": "",
        "analysis_results": "",
        "structured_findings": [],
        "is_vulnerable": False,
        "analysis_success": False,
        "llm_explanation": "",
        "corrected_contract": "",
        "correction_notes": "",
    }

    result = app.invoke(initial_state)

    print("Status:", result["graph_info"])
    print("Findings Count:", len(result["structured_findings"]))

    if result.get("llm_explanation"):
        print("\n--- Auditor Summary ---\n", result["llm_explanation"])

    if result.get("corrected_contract"):
        print("\n--- Corrected Contract ---\n", result["corrected_contract"])

    # Graph visualization
    try:
        png_data = app.get_graph().draw_mermaid_png()
        with open("securesmart_workflow.png", "wb") as f:
            f.write(png_data)
        display(Image(data=png_data))
    except Exception:
        pass