# Diário local de operações com opções

Aplicação local, individual e sem dependências de terceiros. Usa Python 3.11 (biblioteca padrão) e SQLite. Não consulta cotações, não envia seus dados para a internet e não precisa de Excel, OnlyOffice, servidor de banco ou instalação de pacotes.

## Iniciar no Windows

1. Extraia a pasta do projeto em um local permanente, por exemplo `Documentos`.
2. Dê dois cliques em `iniciar_windows.bat`.
3. O navegador abrirá em `http://127.0.0.1:8765`. Se não abrir, copie esse endereço para o navegador.
4. Para encerrar, volte à janela preta aberta pelo `.bat` e pressione `Ctrl+C`.

Requer Python 3.11 ou mais recente instalado e disponível como `python` no Windows. O banco será criado em `data/diario_opcoes.sqlite3`, ao lado do programa. Não mova nem exclua esse banco se quiser manter os registros.

## Iniciar em macOS / Linux

Abra um terminal nesta pasta e execute:

```sh
python3 app.py
```

O app abrirá o navegador. Encerre com `Ctrl+C`. Também é possível rodar `python app.py` se `python` apontar para Python 3.

## Como registrar uma operação

1. Em **Nova estrutura**, informe data de montagem, ativo-objeto, nome da estratégia e, se desejar, capital-base.
2. Adicione as pernas. Para cada uma, registre código/série, call ou put, strike, vencimento, compra ou venda, quantidade, prêmio de entrada, multiplicador e custos totais de entrada.
3. Salve. A quantidade inicial de estruturas é contada pela menor quantidade entre as pernas. Assim, quatro pernas de 200 contratos equivalem a **200 estruturas**, e não 800.
4. No diário, abra **Detalhes** para ver o payoff, cadastrar desmontagens totais ou parciais e atualizar manualmente o preço de referência de uma perna.
5. Para uma rolagem, abra os detalhes da operação e escolha **Adicionar perna / rolagem**. Registre a nova série, tipo, strike, vencimento, lado, quantidade, prêmio, data e custos. A perna antiga continua no histórico com suas saídas; o saldo aberto passa a considerar as pernas ainda ativas. A contagem inicial da operação não é alterada.
5. Use **Exportar backup** com frequência e guarde a cópia fora da pasta do programa. O backup JSON pode ser importado posteriormente; IDs já existentes são ignorados.

## Convenções de cálculo

Prêmios e custos são informados como valores positivos; quantidade e multiplicador também são positivos. Long significa compra; short significa venda.

- **Long / comprado — resultado bruto:** `(prêmio de saída − prêmio de entrada) × quantidade × multiplicador`.
- **Short / vendido — resultado bruto:** `(prêmio de entrada − prêmio de saída) × quantidade × multiplicador`.
- **Resultado realizado líquido:** resultado bruto das saídas registradas, menos os custos de saída e a parcela proporcional dos custos de entrada. Custos de entrada são rateados pela quantidade encerrada de cada perna.
- **Resultado não realizado estimado:** mesma fórmula, usando o preço de referência manual apenas para quantidade ainda aberta. Não é somado ao realizado no painel; aparece separado.
- **Resultado estimado até o vencimento:** resultado realizado até agora mais o payoff intrínseco da quantidade ainda aberta, descontada a parte restante dos custos de entrada. Não prevê valor extrínseco, volatilidade ou execução real.
- **Retorno financeiro por operação:** `resultado realizado líquido ÷ capital-base × 100`. Pode ser consultado mesmo enquanto há pernas abertas; nesse caso, mede apenas o retorno realizado até agora, não o resultado final. Se não informar capital-base, o percentual não é exibido. Em estruturas a crédito, informe uma base de risco apropriada (por exemplo, risco máximo); o crédito recebido não é automaticamente tratado como capital em risco.
- **Retorno financeiro no painel:** `resultado realizado acumulado ÷ soma dos capitais-base informados × 100`. É um retorno ponderado pelo capital-base, não a média simples dos percentuais das operações. O painel informa quantas operações têm base cadastrada; marcas não realizadas ficam fora desse índice.
- **Retorno estimado da operação:** quando todas as pernas abertas têm preço de referência, a tela de detalhes mostra separadamente `(realizado + não realizado estimado) ÷ capital-base × 100`. Não é misturado ao retorno realizado.
- **Contagem de estruturas:** a quantidade inicial usa a menor quantidade entre as pernas da montagem e permanece fixa ao acrescentar pernas de rolagem. Estruturas abertas usam a menor quantidade remanescente entre as pernas ainda ativas; pernas antigas zeradas continuam no histórico, mas não zeram a contagem aberta. Uma estrutura de quatro pernas com 200 contratos em cada perna conta como 200 estruturas, não 800.
- **Acerto/erro:** calculado por registro de operação encerrado (cada registro conta uma vez), com resultado líquido maior/menor que zero dividido pelo total de registros encerrados. Registros zerados aparecem à parte e permanecem no denominador. A quantidade de estruturas é exibida separadamente.
- **Média de gain/loss:** média do resultado líquido por operação encerrada vencedora/perdedora.
- **Resultados mensais:** agrupados pela data de cada encerramento (saída), não pela data de abertura.

O gráfico mostra o payoff projetado na expiração das pernas restantes, somado ao resultado já realizado. Para estruturas com vencimentos diferentes, o desenho assume o vencimento comum final: não modela a evolução intermediária nem o valor temporal das opções. Confira a escala e os pontos de equilíbrio com suas condições reais de contrato.

## Limitações e segurança

- É um aplicativo de um único computador e de um único usuário. Precisa estar rodando para abrir no navegador; `127.0.0.1` não publica o site na internet.
- Os dados ficam no arquivo SQLite local. Se apagar esse arquivo ou os dados da pasta, apaga o diário; faça backups.
- O app não importa automaticamente sua planilha, notas de corretagem ou operações já existentes; esses registros precisam ser cadastrados manualmente.
- Não calcula impostos, margem, exercícios/liquidações em ações, dividendos nem ajustes corporativos. Registre esses componentes separadamente em suas anotações/controles.
- Não fornece recomendação de investimento e não busca cotações.
- Não deixe o arquivo de banco aberto em editores nem tente executar duas cópias do app ao mesmo tempo.

## Testes

Execute `python -m unittest discover -s tests` nesta pasta para testar abertura, encerramento parcial, custos, contagem de estruturas e encerramento total.
