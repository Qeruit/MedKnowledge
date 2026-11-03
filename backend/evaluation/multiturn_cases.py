"""Fixed multi-turn labels against the partial public ActaStim-S demo.

These are engineering regression examples, not clinical validation. Each turn
labels the evidence needed *for that turn*, never a previous model answer.
"""

SCENARIOS = [
    {
        "id": "referent",
        "category": "指代追问",
        "turns": [
            {"question": "ActaStim-S 是什么，适用于谁？", "evidence": ["是什么，适用于谁"],
             "facts": [["成人"], ["腰椎"]]},
            {"question": "那它每天推荐佩戴多久？", "evidence": ["推荐的佩戴时间"],
             "facts": [["24"]]},
        ],
    },
    {
        "id": "topic-switch",
        "category": "话题切换后追问",
        "turns": [
            {"question": "ActaStim-S 电池充满需要多久？", "evidence": ["电池充满需要"],
             "facts": [["3"]]},
            {"question": "ActaStim-S 的存储温度是多少？", "evidence": ["存储温度"],
             "facts": [["-25", "−25"], ["70"]]},
            {"question": "那它最长疗程是多少天？", "evidence": ["推荐的佩戴时间"],
             "facts": [["270"]], "forbiddenPhrases": ["3 小时的疗程", "3小时的疗程"]},
        ],
    },
    {
        "id": "no-evidence",
        "category": "资料不足时拒答",
        "turns": [
            {"question": "ActaStim-S 的存储温度是多少？", "evidence": ["存储温度"],
             "facts": [["-25", "−25"], ["70"]]},
            {"question": "那它的售价和医保报销比例是多少？", "evidence": [], "facts": []},
        ],
    },
]
