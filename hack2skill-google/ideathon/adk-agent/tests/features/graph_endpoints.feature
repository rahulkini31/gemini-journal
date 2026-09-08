Feature: Graph visualization endpoints
  The frontend needs to fetch a signed-in user's own relationship graph and
  emotional-pattern graph directly, without going through a chat turn, so it
  can render them. Both endpoints are read-only, owner-scoped, and never
  return another user's data.

  Scenario: A signed-in user fetches their relationship graph
    Given a signed-in user with two related journal entries
    When they request the relationship graph
    Then the response includes both entries as nodes
    And an edge connects them

  Scenario: A signed-in user fetches their emotional pattern graph
    Given a signed-in user with a repeated "work" leading to "frustrated" pattern
    When they request the emotional pattern graph
    Then the response includes a pattern from "work" to "frustrated"
    And the pattern's mention count is at least 2

  Scenario: A user with no analyzed reflections sees an empty graph, not an error
    Given a signed-in user with no analyzed reflections
    When they request the relationship graph
    Then the response is a successful empty graph

  Scenario: An unauthenticated request is rejected
    When an unauthenticated request is made for the relationship graph
    Then the request is rejected with 401

  Scenario: A user never sees another user's graph data
    Given a signed-in user with two related journal entries
    And a different user with their own unrelated journal entries
    When they request the relationship graph
    Then the response never includes the other user's entries
