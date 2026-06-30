"""Prompt STT italiano — evento Durst Brixen/Bressanone (fuori da stt/ per evitare import circolari)."""

DURST_IT_STT_VOCAB = (
    "Contesto: siamo all'evento Durst presso la sede Durst Group a Brixen/Bressanone, in Alto Adige. "
    "Durst è un'azienda di stampa digitale industriale. "
    "Parole e nomi frequenti: Durst, Durst Group, Brixen, Bressanone, Alto Adige, Südtirol, "
    "reception, accoglienza, visitatori, evento, sede, stampa digitale, benvenuti, orientamento."
)

ITALIAN_WHISPER_PROMPT = (
    "Trascrivi SOLO in italiano. NON tradurre. NON usare spagnolo, inglese o tedesco. "
    f"{DURST_IT_STT_VOCAB} "
    "Wake word: Hey G1 / Ehi G1 (lettera G + numero 1). NON scrivere Bepi, Bepì, Pepi o nomi inventati. "
    "Esempi: hey g1, ehi g1, dove siamo, cos'è Durst, che evento è, buonasera, grazie, "
    "reception, Brixen, Bressanone, accoglienza. "
    "Trascrivi fedelmente le parole pronunciate in italiano. Se non senti italiano chiaro, rispondi vuoto."
)

ITALIAN_WAKE_WHISPER_PROMPT = (
    "Solo italiano. MAI spagnolo o inglese. "
    f"{DURST_IT_STT_VOCAB} "
    "Wake word: 'Hey G1' o 'Ehi G1' — lettera G seguita dal numero 1. "
    "NON è un nome proprio: NON scrivere Bepi, Bepì, Pepi, Gepi. "
    "Trascrivi sempre G1, Hey G1, Ehi G1. "
    "Se non senti italiano chiaro, rispondi vuoto. Non inventare frasi."
)
