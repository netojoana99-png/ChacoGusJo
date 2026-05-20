"""
SaúdeConectada Nordeste — Backend MVP
Hospital Regional Santa Cruz, Petrolina-PE

Módulo de Prontuário Eletrônico (PEI)
Versão: 1.0.0

Contexto de uso:
  - Médicos plantonistas em alta rotatividade e estresse
  - Conectividade instável (quedas de até 20 min/dia)
  - Dados sensíveis de saúde — sujeitos à LGPD (Art. 5, inciso II e Art. 11)

Decisão de persistência:
  Usamos um arquivo JSON local (prontuarios.json) em vez de banco de dados real.
  Justificativa: para o MVP/protótipo, isso elimina a necessidade de configurar
  PostgreSQL, facilita rodar localmente sem infraestrutura e torna o código
  auditável. Em produção, essa camada deve ser substituída pelo PostgreSQL.

Decisão de arquitetura de banco (para produção — documentada aqui conforme requisito):
  - Prontuários/históricos clínicos → PostgreSQL (relacional):
      Dados estruturados com relações (paciente ↔ atendimento ↔ médico),
      necessidade de ACID para integridade clínica, consultas complexas por CPF,
      período, CID etc.
  - Dados de sensores e imagens → NoSQL (ex: MongoDB ou TimescaleDB):
      Volume massivo, schema variável por tipo de sensor, acesso por janela
      temporal (não relacional), alta taxa de escrita. A proposta da empresa
      terceirizada de unificar tudo num único banco é um risco arquitetural
      identificado e NÃO recomendado.
"""

import json
import os
import re
import uuid
from datetime import datetime, timezone

from flask import Flask, jsonify, request
from flask_cors import CORS

# ---------------------------------------------------------------------------
# Inicialização da aplicação
# ---------------------------------------------------------------------------

app = Flask(__name__)

# CORS habilitado para permitir que o frontend (HTML/JS estático) acesse o
# backend mesmo rodando em origens diferentes (ex: file:// ou porta distinta).
# Em produção, restringir origins à lista de domínios autorizados do hospital.
CORS(app)

# Caminho do arquivo de persistência simplificada.
# Usamos a pasta /data relativa ao arquivo app.py para facilitar o Docker
# e a separação de responsabilidades (código vs. dados).
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE_DIR, "..", "data", "prontuarios.json")


# ---------------------------------------------------------------------------
# Helpers de persistência
# ---------------------------------------------------------------------------

def _carregar_prontuarios() -> list:
    """
    Carrega os prontuários do arquivo JSON.

    Retorna lista vazia se o arquivo não existir ainda (primeira execução).
    Isso evita que o sistema quebre no primeiro boot — importante para
    ambientes com conectividade instável onde a inicialização pode ser parcial.
    """
    if not os.path.exists(DATA_FILE):
        return []
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _salvar_prontuarios(prontuarios: list) -> None:
    """
    Persiste a lista de prontuários no arquivo JSON com indentação legível.

    Decisão: escrita atômica não implementada no MVP, mas em produção deve-se
    usar write-to-temp + rename para evitar corrupção em caso de queda de energia
    — risco real dado o cenário de instabilidade elétrica do semiárido.
    """
    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(prontuarios, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Helpers de validação
# ---------------------------------------------------------------------------

def _validar_cpf_formato(cpf: str) -> bool:
    """
    Valida formato básico de CPF: apenas dígitos, 11 caracteres.

    Nota: esta validação verifica apenas o formato, não os dígitos
    verificadores. Para produção, implementar validação completa do algoritmo
    da Receita Federal. CPF é dado pessoal direto (LGPD Art. 5, I) —
    deve ser armazenado com criptografia em produção.
    """
    return bool(re.fullmatch(r"\d{11}", cpf))


def _validar_data_nascimento(data_str: str) -> bool:
    """
    Valida que a data de nascimento está no formato ISO (YYYY-MM-DD) e
    representa uma data real no passado.

    Médicos frequentemente cometem erros de digitação em datas sob estresse.
    Validar no backend garante integridade mesmo se o frontend falhar
    (ex: JS desativado ou requisição direta à API).
    """
    try:
        data = datetime.strptime(data_str, "%Y-%m-%d").date()
        return data < datetime.now(timezone.utc).date()
    except ValueError:
        return False


def _validar_sinais_vitais(sinais: dict) -> tuple[bool, str]:
    """
    Valida sinais vitais dentro de faixas fisiologicamente possíveis.

    Isso previne erros de digitação que poderiam gerar alertas clínicos
    incorretos ou poluir históricos. Os limites usados são extremos
    fisiológicos (não limites de normalidade clínica — essa lógica é do
    módulo de IA / regras clínicas, não do backend de registro).

    Retorna: (valido: bool, mensagem_erro: str)
    """
    erros = []

    pressao = sinais.get("pressao", "")
    # Formato esperado: "120/80" (sistólica/diastólica em mmHg)
    if not re.fullmatch(r"\d{2,3}/\d{2,3}", str(pressao)):
        erros.append("pressao deve estar no formato 'SIS/DIA' (ex: 120/80)")
    else:
        sis, dia = map(int, str(pressao).split("/"))
        if not (50 <= sis <= 300 and 20 <= dia <= 200):
            erros.append("pressao fora de faixas fisiológicas aceitáveis (SIS: 50-300, DIA: 20-200)")

    temperatura = sinais.get("temperatura")
    try:
        temp = float(temperatura)
        if not (30.0 <= temp <= 45.0):
            erros.append("temperatura fora de faixa fisiológica (30.0 a 45.0 °C)")
    except (TypeError, ValueError):
        erros.append("temperatura deve ser um número (ex: 37.5)")

    fc = sinais.get("frequencia_cardiaca")
    try:
        freq = int(fc)
        if not (20 <= freq <= 300):
            erros.append("frequencia_cardiaca fora de faixa fisiológica (20 a 300 bpm)")
    except (TypeError, ValueError):
        erros.append("frequencia_cardiaca deve ser um inteiro (ex: 80)")

    return (len(erros) == 0, "; ".join(erros))


# ---------------------------------------------------------------------------
# Rota: POST /prontuario
# ---------------------------------------------------------------------------

@app.route("/prontuario", methods=["POST"])
def criar_prontuario():
    """
    Registra um novo atendimento médico.

    Decisão de design: aceitamos JSON no body (Content-Type: application/json).
    Isso padroniza a interface com o frontend e futuros integradores (outros
    sistemas do hospital, laboratório, farmácia).

    Campos obrigatórios:
      - nome_paciente (str): nome completo
      - cpf (str): 11 dígitos sem pontuação
      - data_nascimento (str): formato YYYY-MM-DD
      - queixa_principal (str): descrição da queixa
      - sinais_vitais (obj): { pressao, temperatura, frequencia_cardiaca }

    Campos opcionais:
      - observacoes (str): anotações adicionais do médico

    Retorna 201 com o prontuário criado ou 4xx com detalhes do erro.
    """

    # --- 1. Verificar Content-Type ---
    # Rejeitamos requisições sem JSON para evitar erros silenciosos de parse.
    # Código 415: Unsupported Media Type — padrão REST.
    if not request.is_json:
        return jsonify({
            "erro": "Content-Type deve ser application/json"
        }), 415

    dados = request.get_json()

    # --- 2. Validar presença dos campos obrigatórios ---
    campos_obrigatorios = [
        "nome_paciente",
        "cpf",
        "data_nascimento",
        "queixa_principal",
        "sinais_vitais",
    ]
    campos_ausentes = [c for c in campos_obrigatorios if not dados.get(c)]

    if campos_ausentes:
        # 422 Unprocessable Entity: o corpo existe e é JSON válido,
        # mas falha na validação semântica — mais preciso que 400.
        return jsonify({
            "erro": "Campos obrigatórios ausentes ou vazios",
            "campos_ausentes": campos_ausentes,
        }), 422

    # --- 3. Validar CPF ---
    cpf = str(dados["cpf"]).strip().replace(".", "").replace("-", "")
    if not _validar_cpf_formato(cpf):
        return jsonify({
            "erro": "CPF inválido. Informe 11 dígitos numéricos sem pontuação.",
            "recebido": dados["cpf"],
        }), 422

    # --- 4. Validar data de nascimento ---
    if not _validar_data_nascimento(dados["data_nascimento"]):
        return jsonify({
            "erro": "data_nascimento inválida. Use o formato YYYY-MM-DD e uma data no passado.",
            "recebido": dados["data_nascimento"],
        }), 422

    # --- 5. Validar sinais vitais ---
    if not isinstance(dados["sinais_vitais"], dict):
        return jsonify({
            "erro": "sinais_vitais deve ser um objeto JSON com os campos: pressao, temperatura, frequencia_cardiaca"
        }), 422

    sinais_ok, sinais_erro = _validar_sinais_vitais(dados["sinais_vitais"])
    if not sinais_ok:
        return jsonify({
            "erro": "Sinais vitais inválidos",
            "detalhes": sinais_erro,
        }), 422

    # --- 6. Montar e persistir o prontuário ---
    # Geramos um UUID v4 como identificador único do atendimento.
    # Isso é mais seguro que IDs sequenciais (evita enumeração de registros).
    prontuario = {
        "id": str(uuid.uuid4()),
        "nome_paciente": dados["nome_paciente"].strip(),
        "cpf": cpf,  # normalizado (só dígitos)
        "data_nascimento": dados["data_nascimento"],
        "queixa_principal": dados["queixa_principal"].strip(),
        "sinais_vitais": {
            "pressao": dados["sinais_vitais"]["pressao"],
            "temperatura": float(dados["sinais_vitais"]["temperatura"]),
            "frequencia_cardiaca": int(dados["sinais_vitais"]["frequencia_cardiaca"]),
        },
        "observacoes": dados.get("observacoes", "").strip(),
        # Timestamp em UTC com timezone explícito.
        # LGPD exige rastreabilidade de quando o dado foi coletado.
        "registrado_em": datetime.now(timezone.utc).isoformat(),
    }

    prontuarios = _carregar_prontuarios()
    prontuarios.append(prontuario)
    _salvar_prontuarios(prontuarios)

    # 201 Created com o recurso criado no body — padrão REST.
    # O header Location apontaria para /prontuarios/<id> em uma API completa.
    return jsonify({
        "mensagem": "Atendimento registrado com sucesso.",
        "prontuario": prontuario,
    }), 201


# ---------------------------------------------------------------------------
# Rota: GET /prontuarios
# ---------------------------------------------------------------------------

@app.route("/prontuarios", methods=["GET"])
def listar_prontuarios():
    """
    Retorna todos os atendimentos registrados, ordenados do mais recente ao mais antigo.

    Decisão de design: retornamos os registros em ordem decrescente de
    data/hora porque plantonistas precisam ver os atendimentos mais recentes
    primeiro — contexto de urgência, não de auditoria histórica.

    Em produção: implementar paginação (parâmetros ?pagina=1&limite=20) para
    evitar degradação de performance com grandes volumes. Com 800 mil pacientes
    potenciais, uma lista não paginada seria inviável.

    Retorna 200 com lista (pode ser vazia) ou 500 em caso de falha de leitura.
    """
    try:
        prontuarios = _carregar_prontuarios()
    except (json.JSONDecodeError, IOError) as e:
        # 500 Internal Server Error: falha de infraestrutura, não do cliente.
        # Log do erro seria essencial em produção (ex: integração com Sentry).
        return jsonify({
            "erro": "Falha ao acessar o armazenamento de prontuários.",
            "detalhe": str(e),
        }), 500

    # Ordenação decrescente por data de registro
    prontuarios_ordenados = sorted(
        prontuarios,
        key=lambda p: p.get("registrado_em", ""),
        reverse=True,
    )

    return jsonify({
        "total": len(prontuarios_ordenados),
        "prontuarios": prontuarios_ordenados,
    }), 200


# ---------------------------------------------------------------------------
# Rota: GET /prontuarios/<cpf>
# ---------------------------------------------------------------------------

@app.route("/prontuarios/<cpf>", methods=["GET"])
def buscar_por_cpf(cpf: str):
    """
    Retorna o histórico de atendimentos de um paciente pelo CPF.

    Decisão de design: CPF é passado como parâmetro de rota (path param),
    não como query string, porque identifica o recurso em si — segue o
    princípio REST de URLs como identificadores de recursos.

    Consideração LGPD (Art. 11): CPF vinculado a histórico clínico é dado
    sensível. Em produção, esta rota deve exigir autenticação (JWT/OAuth2)
    e autorização (médico logado pode ver apenas pacientes do seu atendimento
    ou do plantão atual). Logar o acesso é obrigatório para fins de auditoria.

    Retorna:
      200 — histórico encontrado (pode conter múltiplos atendimentos)
      404 — nenhum atendimento encontrado para o CPF
      422 — CPF com formato inválido
    """

    # Normalizar e validar CPF recebido na URL
    cpf_normalizado = cpf.strip().replace(".", "").replace("-", "")

    if not _validar_cpf_formato(cpf_normalizado):
        return jsonify({
            "erro": "CPF inválido. Informe 11 dígitos numéricos.",
            "recebido": cpf,
        }), 422

    try:
        todos = _carregar_prontuarios()
    except (json.JSONDecodeError, IOError) as e:
        return jsonify({
            "erro": "Falha ao acessar o armazenamento de prontuários.",
            "detalhe": str(e),
        }), 500

    # Filtrar atendimentos do paciente e ordenar cronologicamente (desc)
    historico = sorted(
        [p for p in todos if p.get("cpf") == cpf_normalizado],
        key=lambda p: p.get("registrado_em", ""),
        reverse=True,
    )

    if not historico:
        # 404 Not Found: não há prontuários para este CPF.
        # Importante: NÃO revelar se o CPF existe no sistema sem histórico de
        # atendimentos vs. se nunca foi cadastrado — em produção, a distinção
        # pode ser uma vulnerabilidade de enumeração (LGPD + segurança).
        return jsonify({
            "erro": "Nenhum atendimento encontrado para o CPF informado.",
            "cpf": cpf_normalizado,
        }), 404

    return jsonify({
        "cpf": cpf_normalizado,
        "nome_paciente": historico[0]["nome_paciente"],
        "total_atendimentos": len(historico),
        "historico": historico,
    }), 200


# ---------------------------------------------------------------------------
# Handlers de erro globais
# ---------------------------------------------------------------------------

@app.errorhandler(404)
def nao_encontrado(e):
    """
    Captura rotas não definidas e retorna JSON em vez da página HTML padrão do Flask.
    Importante para que o frontend possa tratar erros de forma consistente.
    """
    return jsonify({"erro": "Rota não encontrada.", "detalhe": str(e)}), 404


@app.errorhandler(405)
def metodo_nao_permitido(e):
    """
    Retorna erro estruturado quando o método HTTP não é suportado pela rota.
    Ex: GET /prontuario em vez de POST /prontuario.
    """
    return jsonify({"erro": "Método HTTP não permitido para esta rota.", "detalhe": str(e)}), 405


@app.errorhandler(500)
def erro_interno(e):
    """
    Handler genérico para erros não tratados.
    Em produção: integrar com sistema de monitoramento (ex: Sentry, Datadog).
    """
    return jsonify({"erro": "Erro interno do servidor.", "detalhe": str(e)}), 500


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # debug=True apenas para desenvolvimento local.
    # Em produção: usar Gunicorn + Nginx. NUNCA rodar Flask dev server em produção.
    # host="0.0.0.0" necessário para aceitar conexões de outros dispositivos
    # na mesma rede do hospital (tablets, notebooks dos plantonistas).
    app.run(debug=True, host="0.0.0.0", port=5000)
