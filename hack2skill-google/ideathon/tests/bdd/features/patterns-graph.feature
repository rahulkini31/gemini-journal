Feature: Visualizing reflection patterns
  A signed-in user can open a patterns view from the journal and see two
  graphs built from their own saved reflections: which entries relate to
  each other, and which things tend to cause which feelings.

  Background:
    Given I am signed in

  Scenario: Viewing the relationship graph
    Given my relationship graph has two connected reflections
    When I open the patterns view
    Then I see a graph of related reflections
    And the graph shows 2 reflections

  Scenario: Viewing the emotional pattern graph
    Given my emotional pattern graph has a "work" to "frustrated" pattern
    When I open the patterns view
    And I switch to the emotional patterns tab
    Then I see a graph of triggers and emotions
    And I see the pattern "work tends to bring up frustrated"

  Scenario: No analyzed reflections yet
    Given I have no analyzed reflections
    When I open the patterns view
    Then I see a message instead of a graph
