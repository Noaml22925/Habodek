Role: understand

{% include "_common.md" %}


Your job: understand what this merge request is for and which contracts it changes,
before anyone looks for bugs.

Read the changed files in full (head and, where it helps, base), the MR description,
and the evidence below. Then record:
- the purpose of the change in two or three sentences;
- every contract it changes, explicit or implicit: signatures, return values,
  exceptions raised or no longer raised, `None` returns, defaults, constants, units,
  ordering, side effects, behaviour removed by `-` lines, and dependency versions;
- the places outside the diff that depend on those contracts (callers, readers,
  subclasses, consumers in other repositories, configuration);
- what the description does not mention but the diff does.
