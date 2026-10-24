"""Single source for the habit-guard and dossier reminders.

The full packet (mcp_chat_prompts.packet) and the compact wire packet
(mcp_chat_output.compact_wait) emit the same decisions in two phrasings;
both live here so a wording change is made once, and the compact side
keeps its deliberately shorter text.
"""

DOSSIER_REMINDER = '已自动加载发言群友的小本本画像，可自然使用其外号、XP喜好与核心记忆针对性调侃（偶尔可用“我小本本上可记着...”），严禁客服腔。'


def habit_hints(state, *, brief=False):
    """The repeated-phrase guard as a one-item list, full or compact wording."""
    if state.get('repeated_phrases'):
        phrases_str = '、'.join(f'“{p}”' for p in state['repeated_phrases'])
        if brief:
            return [f'【防语癖固化】最近发言连续出现重复尾缀（如 {phrases_str}），本次请务必变换句式与表达，严禁继续机械使用同一词尾。']
        return [f'【防语癖固化】注意：最近发言多次出现重复尾缀/单字口癖（如 {phrases_str}）。本次发言请务必更换句式、语气与词尾，严禁机械重复既有口癖，保持表达丰富鲜活，防止自回归对话陷入复读固化。']
    if brief:
        return ['【表达去模板化】拒绝单一尾缀与口癖固化（如连续以“草”、“难绷”、“乐”结尾），根据语境灵活变换句式与语气。']
    return ['【表达去模板化】拒绝单一尾缀与口癖固化（如连续以“草”、“难绷”、“乐”单字或结尾）。每次发言根据当前语境灵活变换句式、语气与字数，可以反问、陈述细节、轻度调侃或使用表情包，杜绝复读机式自回归强化。']
