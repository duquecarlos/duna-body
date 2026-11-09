"""BODY-ASISTENTE-PROVEEDOR-1: the tool loop, conversations, usage rows and daily ceilings.
Uses a fake `Proveedor` that answers from a scripted list of `Respuesta` objects -- the loop
itself never knows which vendor adapter would have produced them."""
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from tests._db import open_conn

from body import config, db, goals, identity, web
from body.agente import ajustes, bucle, cliente, conversacion, herramientas, uso


def _conn(case: unittest.TestCase):
    c = open_conn(case, Path(tempfile.mkdtemp(dir=TMP)) / "a.db")
    db.init(c)
    return c


def _seed(conn, person_id: int = 1) -> None:
    for i in range(20):
        day = f"2026-10-{i + 1:02d}"
        db.set_metric(conn, day, "resting_hr", 58 - i % 3, "garmin", person_id)
    conn.commit()


class FakeProveedor(cliente.Proveedor):
    """Answers from a scripted list of Respuesta, one per call to `responder`."""

    def __init__(self, respuestas: list[cliente.Respuesta]):
        self._respuestas = list(respuestas)
        self.llamadas = 0

    def responder(self, sistema, mensajes, herramientas):
        self.llamadas += 1
        if not self._respuestas:
            raise AssertionError("FakeProveedor ran out of scripted responses")
        return self._respuestas.pop(0)


def _texto(s: str, uso=None) -> cliente.Respuesta:
    return cliente.Respuesta(bloques=[cliente.BloqueTexto(s)], motivo_fin="end_turn", uso=uso or cliente.Uso())


def _llamada(nombre: str, params: dict | None = None, call_id: str = "call_1", uso=None) -> cliente.Respuesta:
    return cliente.Respuesta(bloques=[cliente.LlamadaHerramienta(call_id, nombre, params or {})],
                              motivo_fin="tool_use", uso=uso or cliente.Uso())


class TwoRoundToolLoopTest(unittest.TestCase):
    def test_streams_herramienta_then_texto_then_reports_a_final_answer(self):
        conn = _conn(self)
        _seed(conn)
        prov = FakeProveedor([
            _llamada("leer_hoy", {}, uso=cliente.Uso(entrada=10, salida=5)),
            _texto("Tu FC en reposo hoy es buena.", uso=cliente.Uso(entrada=20, salida=8)),
        ])
        resultado = bucle.ejecutar(conn, 1, prov, "eres el asistente",
                                   [{"rol": "usuario", "bloques": [{"tipo": "texto", "texto": "¿cómo estoy?"}]}])
        self.assertEqual([type(e).__name__ for e in resultado.eventos],
                         ["EventoHerramienta", "EventoTexto"])
        self.assertEqual(resultado.eventos[0].nombre, "leer_hoy")
        self.assertEqual(resultado.texto_final, "Tu FC en reposo hoy es buena.")
        self.assertEqual(resultado.rondas, 2)
        self.assertEqual(resultado.lecturas, 1)
        self.assertEqual(resultado.uso_total, cliente.Uso(entrada=30, salida=13))


class RondasCapTest(unittest.TestCase):
    def test_nine_rounds_of_a_refused_tool_call_hits_the_eight_round_cap(self):
        conn = _conn(self)
        _seed(conn)
        # Every call names an unknown tool, so each is refused by ejecutar() (a ToolError, no
        # actual read) -- the loop keeps going round after round without ever spending read
        # budget, so it is the ROUND cap, not the read cap, that trips here.
        prov = FakeProveedor([_llamada("leer_no_existe", {}, call_id=f"c{i}") for i in range(9)])
        with self.assertRaises(bucle.LimiteRondas) as ctx:
            bucle.ejecutar(conn, 1, prov, "sis",
                           [{"rol": "usuario", "bloques": [{"tipo": "texto", "texto": "x"}]}])
        self.assertEqual(ctx.exception.rondas, bucle.MAX_RONDAS)
        self.assertEqual(ctx.exception.lecturas, 0)
        self.assertEqual(prov.llamadas, bucle.MAX_RONDAS)


class LecturasCapTest(unittest.TestCase):
    def test_six_tool_calls_in_one_round_hits_the_five_read_cap(self):
        conn = _conn(self)
        _seed(conn)
        respuesta_con_seis_llamadas = cliente.Respuesta(
            bloques=[cliente.LlamadaHerramienta(f"c{i}", "leer_hoy", {}) for i in range(6)],
            motivo_fin="tool_use", uso=cliente.Uso(),
        )
        prov = FakeProveedor([respuesta_con_seis_llamadas])
        with self.assertRaises(bucle.LimiteRondas) as ctx:
            bucle.ejecutar(conn, 1, prov, "sis",
                           [{"rol": "usuario", "bloques": [{"tipo": "texto", "texto": "x"}]}])
        self.assertEqual(ctx.exception.lecturas, bucle.MAX_LECTURAS)


class NivelDatosEnforcementTest(unittest.TestCase):
    def test_tool_not_offered_at_the_current_nivel_is_still_refused_by_ejecutar(self):
        """nivel_datos='ninguno' still offers leer_turnos/leer_ajustes (their OWN nivel is
        'ninguno' -- herramientas.disponibles is not literally empty there), plus every
        'propuesta' tool (BODY-ASISTENTE-PROPUESTAS-1 decision 2: proposal tools carry
        nivel='ninguno' too, since they never read health data -- they stage a change for the
        person to apply, so nivel_datos does not gate them). What the current nivel refuses is a
        tool ABOVE it that the model calls anyway: ejecutar() refuses it (enforcement at
        dispatch, decision 2), and the loop turns that into a tool-result error fed back to the
        model rather than raising."""
        conn = _conn(self)
        _seed(conn)
        ajustes.set_nivel_datos(conn, 1, "ninguno")
        ofrecidas = {h["nombre"] for h in herramientas.disponibles("ninguno")}
        self.assertEqual(ofrecidas, {
            "leer_turnos", "leer_ajustes", "leer_tutoriales",
            "proponer_meta", "proponer_cambio_meta", "proponer_confirmar_turnos", "proponer_idioma",
            "planear_entreno", "registrar_entreno", "registrar_medida", "marcar_habito",
            "proponer_experimento", "marcar_experimento",
        })

        prov = FakeProveedor([
            _llamada("leer_hoy", {}),  # forced: not offered at nivel 'ninguno'
            _texto("vale"),
        ])
        resultado = bucle.ejecutar(conn, 1, prov, "sis",
                                   [{"rol": "usuario", "bloques": [{"tipo": "texto", "texto": "x"}]}])
        self.assertEqual(resultado.texto_final, "vale")
        resultado_herramienta = resultado.nuevos_mensajes[1]["bloques"][0]
        self.assertIn("error", resultado_herramienta)

    def test_resumen_hides_completo_only_tools(self):
        conn = _conn(self)
        _seed(conn)
        ajustes.set_nivel_datos(conn, 1, "resumen")
        nombres = {h["nombre"] for h in herramientas.disponibles(ajustes.nivel_datos(conn, 1))}
        self.assertNotIn("leer_dia", nombres)
        self.assertNotIn("leer_hoy", nombres)
        self.assertIn("leer_tendencias", nombres)


class ConversacionAjenaTest(unittest.TestCase):
    def setUp(self):
        self.orig_db_path = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", self.orig_db_path))
        config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "conv.db"
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        db.init(self.conn)
        self.conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'B')")
        self.conn.commit()

    def test_another_persons_conversation_id_is_refused(self):
        otra = conversacion.crear(self.conn, 2)
        with self.assertRaises(conversacion.ConversacionAjena):
            conversacion.verificar_propiedad(self.conn, otra, 1)

    def test_unknown_conversation_id_is_refused(self):
        with self.assertRaises(conversacion.ConversacionAjena):
            conversacion.verificar_propiedad(self.conn, 999, 1)

    def test_endpoint_404s_on_another_persons_conversation(self):
        otra = conversacion.crear(self.conn, 2)
        self.conn.commit()
        self.addCleanup(web.app.dependency_overrides.clear)
        web.app.dependency_overrides[identity.current_person_id] = lambda: 1
        client = TestClient(web.app, follow_redirects=False)
        r = client.post("/asistente/preguntar", json={"pregunta": "hola", "conversacion_id": otra})
        self.assertEqual(r.status_code, 404)


def _conteo_tope_env():
    guardado = os.environ.get("BODY_AGENTE_CONTEO_DIARIO")
    os.environ["BODY_AGENTE_CONTEO_DIARIO"] = "200"

    def _restaurar():
        if guardado is None:
            os.environ.pop("BODY_AGENTE_CONTEO_DIARIO", None)
        else:
            os.environ["BODY_AGENTE_CONTEO_DIARIO"] = guardado
    return _restaurar


class ConteoBreakerTest(unittest.TestCase):
    """Uses "modelo-sin-precio", not the real default model: BODY-AGENTE-PRECIOS-1 ships a real
    seed for claude-sonnet-5-5, so a model that must stay unpriced to exercise the count breaker
    needs a name the seed never prices."""

    def setUp(self):
        self.conn = _conn(self)
        self.addCleanup(_conteo_tope_env())

    def test_the_201st_question_today_is_refused_without_calling_the_provider(self):
        for _ in range(200):
            uso.registrar(self.conn, 1, "anthropic", "modelo-sin-precio", cliente.Uso(entrada=1, salida=1),
                          1, "texto", None)
        with self.assertRaises(uso.TopeRefusal):
            uso.verificar_tope(self.conn, 1, "anthropic", "modelo-sin-precio")

    def test_under_the_count_ceiling_is_allowed(self):
        for _ in range(199):
            uso.registrar(self.conn, 1, "anthropic", "modelo-sin-precio", cliente.Uso(entrada=1, salida=1),
                          1, "texto", None)
        uso.verificar_tope(self.conn, 1, "anthropic", "modelo-sin-precio")  # does not raise


class SpendCeilingTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)
        self.conn.execute(
            "INSERT INTO precio_modelo (proveedor, modelo, tipo, micro_usd_por_millon) VALUES "
            "('anthropic', 'modelo-con-precio', 'entrada', 3000000), "
            "('anthropic', 'modelo-con-precio', 'salida', 15000000), "
            "('anthropic', 'modelo-con-precio', 'cache_lectura', 300000), "
            "('anthropic', 'modelo-con-precio', 'cache_escritura', 3750000)"
        )
        self.conn.commit()
        self.addCleanup(_env_tope_diario())

    def test_a_priced_model_over_the_spend_ceiling_refuses(self):
        # 1 USD = 1_000_000 micro_usd, default ceiling is 2 USD -- one huge-output row blows past it.
        uso.registrar(self.conn, 1, "anthropic", "modelo-con-precio",
                      cliente.Uso(entrada=0, salida=1_000_000), 1, "texto", None)
        with self.assertRaises(uso.TopeRefusal):
            uso.verificar_tope(self.conn, 1, "anthropic", "modelo-con-precio")

    def test_a_model_with_no_price_falls_back_to_the_count_breaker_not_zero_cost(self):
        # No price row at all for this model: spending-based cost would be 0 forever, so the
        # ceiling must fall back to counting questions instead of staying open (amendment 5).
        for _ in range(200):
            uso.registrar(self.conn, 1, "anthropic", "modelo-sin-precio",
                          cliente.Uso(entrada=10_000_000, salida=10_000_000), 1, "texto", None)
        with self.assertRaises(uso.TopeRefusal):
            uso.verificar_tope(self.conn, 1, "anthropic", "modelo-sin-precio")


def _env_tope_diario():
    guardado = os.environ.get("BODY_AGENTE_TOPE_DIARIO_MICRO_USD")

    def _restaurar():
        if guardado is None:
            os.environ.pop("BODY_AGENTE_TOPE_DIARIO_MICRO_USD", None)
        else:
            os.environ["BODY_AGENTE_TOPE_DIARIO_MICRO_USD"] = guardado
    return _restaurar


class TopeFailsClosedTest(unittest.TestCase):
    def test_a_failing_ceiling_read_refuses(self):
        conn = _conn(self)
        conn.close()  # a closed connection makes the ceiling's own read fail
        with self.assertRaises(uso.TopeRefusal):
            uso.verificar_tope(conn, 1, "anthropic", "claude-sonnet-5-5")


def _insertar_fila_uso(conn, creado_en: str, modelo: str = "modelo-sin-precio", person_id: int = 1) -> None:
    conn.execute(
        "INSERT INTO uso_agente (person_id, creado_en, proveedor, modelo, entrada, salida, "
        "cache_lectura, cache_escritura, rondas, motivo_fin, codigo_error, es_pregunta, superficie) "
        "VALUES (?, ?, 'anthropic', ?, 1, 1, 0, 0, 1, 'texto', NULL, 1, 'asistente')",
        (person_id, creado_en, modelo),
    )
    conn.commit()


class LocalDayBoundaryTest(unittest.TestCase):
    """Fix (b): `verificar_tope` counts today's rows over [local midnight, next local midnight) in
    the person's OWN timezone (default America/Bogota, UTC-5, no DST) -- never UTC's calendar day.
    "Now" is frozen just after Bogota local midnight, so the UTC calendar day is the same for both
    rows below while their BOGOTA calendar day differs."""

    def setUp(self):
        self.conn = _conn(self)
        self.addCleanup(_conteo_tope_env())
        os.environ["BODY_AGENTE_CONTEO_DIARIO"] = "1"
        self._ahora = datetime(2026, 11, 2, 5, 30, tzinfo=timezone.utc)  # Bogota 2026-11-02 00:30

    def test_a_row_from_yesterdays_bogota_day_is_not_counted_as_today(self):
        # Bogota 2026-11-01 23:30 -- UTC calendar day is already Nov 2, but Bogota's is still Nov 1.
        _insertar_fila_uso(self.conn, "2026-11-02T04:30:00+00:00")
        with patch("body.agente.uso._ahora_utc", return_value=self._ahora):
            uso.verificar_tope(self.conn, 1, "anthropic", "modelo-sin-precio")  # 0 rows today: no raise

    def test_a_row_from_todays_bogota_day_is_counted(self):
        # Bogota 2026-11-02 00:15 -- today, local.
        _insertar_fila_uso(self.conn, "2026-11-02T05:15:00+00:00")
        with patch("body.agente.uso._ahora_utc", return_value=self._ahora):
            with self.assertRaises(uso.TopeRefusal):
                uso.verificar_tope(self.conn, 1, "anthropic", "modelo-sin-precio")


class RowByRowPricingTest(unittest.TestCase):
    """Fix (c): today's spend sums each row at ITS OWN (proveedor, modelo) prices, never at the
    current model's rate applied to every row."""

    def setUp(self):
        self.conn = _conn(self)
        self.conn.execute(
            "INSERT INTO precio_modelo (proveedor, modelo, tipo, micro_usd_por_millon) VALUES "
            "('anthropic', 'modelo-a', 'entrada', 0), "
            "('anthropic', 'modelo-a', 'salida', 1000000), "
            "('anthropic', 'modelo-a', 'cache_lectura', 0), "
            "('anthropic', 'modelo-a', 'cache_escritura', 0), "
            "('anthropic', 'modelo-b', 'entrada', 0), "
            "('anthropic', 'modelo-b', 'salida', 2000000), "
            "('anthropic', 'modelo-b', 'cache_lectura', 0), "
            "('anthropic', 'modelo-b', 'cache_escritura', 0)"
        )
        self.conn.commit()
        self.addCleanup(_env_tope_diario())
        self.addCleanup(_conteo_tope_env())

    def test_two_priced_models_today_are_summed_each_at_its_own_rate(self):
        uso.registrar(self.conn, 1, "anthropic", "modelo-a", cliente.Uso(entrada=0, salida=1_000_000),
                      1, "texto", None)
        uso.registrar(self.conn, 1, "anthropic", "modelo-b", cliente.Uso(entrada=0, salida=1_000_000),
                      1, "texto", None)
        # Row-by-row: $1 (modelo-a) + $2 (modelo-b) = $3 = 3_000_000 micro-USD. A ceiling of
        # 2_500_000 sits BELOW the correct sum but ABOVE what pricing every row at modelo-a's own
        # rate alone would give ($1 + $1 = 2_000_000) -- so this discriminates the fix.
        os.environ["BODY_AGENTE_TOPE_DIARIO_MICRO_USD"] = "2500000"
        with self.assertRaises(uso.TopeRefusal):
            uso.verificar_tope(self.conn, 1, "anthropic", "modelo-a")

    def test_one_unpriced_row_today_falls_back_to_the_count_breaker(self):
        os.environ["BODY_AGENTE_CONTEO_DIARIO"] = "1"
        uso.registrar(self.conn, 1, "anthropic", "modelo-a", cliente.Uso(entrada=0, salida=1), 1, "texto", None)
        uso.registrar(self.conn, 1, "anthropic", "modelo-sin-precio", cliente.Uso(entrada=0, salida=1),
                      1, "texto", None)
        with self.assertRaises(uso.TopeRefusal):
            uso.verificar_tope(self.conn, 1, "anthropic", "modelo-a")


class UsoRegistradoTest(unittest.TestCase):
    def test_one_row_per_question_with_the_fakes_counts(self):
        conn = _conn(self)
        _seed(conn)
        prov = FakeProveedor([_texto("hola", uso=cliente.Uso(entrada=11, salida=4, cache_lectura=2,
                                                              cache_escritura=1))])
        resultado = bucle.ejecutar(conn, 1, prov, "sis",
                                   [{"rol": "usuario", "bloques": [{"tipo": "texto", "texto": "x"}]}])
        uso.registrar(conn, 1, "anthropic", "claude-sonnet-5-5", resultado.uso_total, resultado.rondas,
                      "texto", None)
        rows = conn.execute("SELECT * FROM uso_agente").fetchall()
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r["entrada"], r["salida"], r["cache_lectura"], r["cache_escritura"]), (11, 4, 2, 1))
        self.assertEqual(r["rondas"], 1)
        self.assertEqual(r["motivo_fin"], "texto")
        self.assertIsNone(r["codigo_error"])
        self.assertEqual(r["es_pregunta"], 1)


class RequestLogHasNoQuestionTextTest(unittest.TestCase):
    def setUp(self):
        self.orig_db_path = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", self.orig_db_path))
        config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "log.db"
        conn = db.connect()
        db.init(conn)
        conn.close()
        self.addCleanup(web.app.dependency_overrides.clear)
        web.app.dependency_overrides[identity.current_person_id] = lambda: 1
        self.client = TestClient(web.app, follow_redirects=False)
        # Never let a real key on this machine's environment make this test call a real provider.
        claves = ("BODY_AGENTE_ANTHROPIC_API_KEY", "BODY_AGENTE_OPENAI_API_KEY", "BODY_AGENTE_GEMINI_API_KEY")
        guardadas = {k: os.environ.pop(k, None) for k in claves}
        self.addCleanup(lambda: [os.environ.update({k: v}) if v is not None else os.environ.pop(k, None)
                                 for k, v in guardadas.items()])

    def test_no_provider_configured_answers_a_plain_error_event_with_no_question_text(self):
        with self.assertLogs("body.request", level="INFO") as captura:
            r = self.client.post("/asistente/preguntar",
                                 json={"pregunta": "mi secreto de salud muy privado", "conversacion_id": None})
        self.assertEqual(r.status_code, 200)
        self.assertIn("no_configurado", r.text)
        texto_logs = " ".join(captura.output)
        self.assertNotIn("mi secreto de salud muy privado", texto_logs)
        self.assertIn("/asistente/preguntar", texto_logs)


if __name__ == "__main__":
    unittest.main()
