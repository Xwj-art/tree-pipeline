# contract.yaml (template)
#
# Contract-first boundary specification for multi-module development.
# This file is designed to be easy to diff/review and safe to freeze early.

version: "{{contract_version}}"
generated_at: "{{generated_at}}"

modules:
  # Each module declares what it PROVIDES and what it REQUIRES from others.
  # A "signature" is an intentionally stable string representation.
  # Use semantic versioning for cross-module compatibility.
  - name: "{{module_name}}"
    description: "{{module_description}}"
    provides:
      functions:
        # - id: core.user.get
        #   signature: "get_user(user_id: str) -> User"
        #   since: "1.0.0"
        #   errors:
        #     - code: USER_NOT_FOUND
        #       http_status: 404
        #       retryable: false
      http_endpoints:
        # - id: api.GET_/v1/users/{id}
        #   method: GET
        #   path: /v1/users/{id}
        #   request_schema: {}
        #   response_schema: {}
        #   errors: []
    requires:
      functions:
        # - id: core.user.get
        #   signature: "get_user(user_id: str) -> User"
        #   constraint: ">=1.0.0,<2.0.0"
      http_endpoints: []

