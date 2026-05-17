# Coin Price by IDs

> This endpoint allows you to **query the prices of one or more coins by using their unique Coin API IDs**



## OpenAPI

````yaml v3.0.1/reference/api-reference/coingecko-demo.json get /simple/price
openapi: 3.0.0
info:
  title: CoinGecko Demo API
  version: 3.0.0
servers:
  - url: https://api.coingecko.com/api/v3
security:
  - apiKeyAuth: []
  - apiKeyQueryParam: []
paths:
  /simple/price:
    get:
      tags:
        - Simple
      summary: Coin Price by IDs
      description: >-
        This endpoint allows you to **query the prices of one or more coins by
        using their unique Coin API IDs**
      operationId: simple-price
      parameters:
        - name: vs_currencies
          in: query
          description: >-
            target currency of coins, comma-separated if querying more than 1
            currency. 
             *refers to [`/simple/supported_vs_currencies`](/v3.0.1/reference/simple-supported-currencies).
          required: true
          schema:
            type: string
            default: usd
          examples:
            one value:
              value: usd
            multiple values:
              value: usd,eur
        - name: ids
          in: query
          description: |-
            coins' IDs, comma-separated if querying more than 1 coin. 
             *refers to [`/coins/list`](/v3.0.1/reference/coins-list).
          required: false
          schema:
            type: string
            default: bitcoin
          examples:
            one value:
              value: bitcoin
            multiple values:
              value: bitcoin,ethereum
        - name: names
          in: query
          description: coins' names, comma-separated if querying more than 1 coin.
          required: false
          schema:
            type: string
            default: Bitcoin
          examples:
            one value:
              value: Bitcoin
            multiple values:
              value: Bitcoin,Ethereum
        - name: symbols
          in: query
          description: coins' symbols, comma-separated if querying more than 1 coin.
          required: false
          schema:
            type: string
            default: btc
          examples:
            one value:
              value: btc
            multiple values:
              value: btc,eth
        - name: include_tokens
          in: query
          description: |-
            for `symbols` lookups, specify `all` to include all matching tokens 
             Default `top` returns top-ranked tokens (by market cap or volume)
          required: false
          schema:
            type: string
            enum:
              - top
              - all
        - name: include_market_cap
          in: query
          description: 'include market capitalization, default: false'
          required: false
          schema:
            type: boolean
        - name: include_24hr_vol
          in: query
          description: 'include 24hr volume, default: false'
          required: false
          schema:
            type: boolean
        - name: include_24hr_change
          in: query
          description: 'include 24hr change percentage, default: false'
          schema:
            type: boolean
        - name: include_last_updated_at
          in: query
          description: 'include last updated price time in UNIX, default: false'
          required: false
          schema:
            type: boolean
        - name: precision
          in: query
          description: 'decimal place for currency price value '
          required: false
          schema:
            type: string
            enum:
              - full
              - '0'
              - '1'
              - '2'
              - '3'
              - '4'
              - '5'
              - '6'
              - '7'
              - '8'
              - '9'
              - '10'
              - '11'
              - '12'
              - '13'
              - '14'
              - '15'
              - '16'
              - '17'
              - '18'
      responses:
        '200':
          description: price(s) of cryptocurrency
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/SimplePrice'
components:
  schemas:
    SimplePrice:
      type: object
      additionalProperties:
        $ref: '#/components/schemas/SimplePriceData'
      example:
        bitcoin:
          usd: 67187.3358936566
          usd_market_cap: 1317802988326.25
          usd_24h_vol: 31260929299.5248
          usd_24h_change: 3.63727894677354
          last_updated_at: 1711356300
    SimplePriceData:
      type: object
      properties:
        usd:
          type: number
          description: price in USD
        usd_market_cap:
          type: number
          description: market cap in USD
        usd_24h_vol:
          type: number
          description: 24hr volume in USD
        usd_24h_change:
          type: number
          description: 24hr change percentage in USD
        last_updated_at:
          type: number
          description: last updated timestamp
  securitySchemes:
    apiKeyAuth:
      type: apiKey
      in: header
      name: x-cg-demo-api-key
    apiKeyQueryParam:
      type: apiKey
      in: query
      name: x_cg_demo_api_key

````

---

> To find navigation and other pages in this documentation, fetch the llms.txt file at: https://docs.coingecko.com/llms.txt