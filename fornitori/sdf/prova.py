import sqlite3
c = sqlite3.connect("fornitori/sdf/sdf.db")
c.row_factory = sqlite3.Row

# il crawl è finito?
print([dict(r) for r in c.execute("SELECT key FROM crawl_state WHERE key LIKE 'mod:%'")])
print(c.execute("""SELECT COUNT(*) FROM part p
  JOIN model_drawing md ON md.revision_id=p.revision_id
  WHERE md.brand='SAME' AND md.model_id=23672""").fetchone()[0])

# tutte le tavole che parlano di zavorra
for r in c.execute("""SELECT DISTINCT d.revision_id, d.name, g.name AS gruppo, s.name AS sub
  FROM drawing d
  JOIN model_drawing md ON md.revision_id=d.revision_id
  LEFT JOIN grp g ON g.row_id=md.group_id
  LEFT JOIN subgroup s ON s.row_id=md.subgroup_id
  WHERE md.model_id=23672 AND (d.name LIKE '%ZAVORR%' OR s.name LIKE '%ZAVORR%')"""):
    print(dict(r))

# e tutti i pezzi la cui descrizione contiene zavorra
for r in c.execute("""SELECT p.code, p.description, d.name AS tavola
  FROM part p JOIN drawing d ON d.revision_id=p.revision_id
  JOIN model_drawing md ON md.revision_id=p.revision_id
  WHERE md.model_id=23672 AND p.description LIKE '%zavorr%'"""):
    print(dict(r))

for r in c.execute("""SELECT g.name AS gruppo, COUNT(DISTINCT d.revision_id) AS tavole,
                             COUNT(p.code) AS pezzi
  FROM model_drawing md
  JOIN drawing d ON d.revision_id = md.revision_id
  LEFT JOIN grp g ON g.row_id = md.group_id
  LEFT JOIN part p ON p.revision_id = d.revision_id
  WHERE md.model_id = 23672
  GROUP BY g.name ORDER BY pezzi DESC"""):
    print(dict(r))

print([dict(r) for r in c.execute("""
  SELECT DISTINCT md.group_id, g.name
  FROM model_drawing md LEFT JOIN grp g ON g.row_id = md.group_id
  WHERE md.model_id = 23672 AND (g.name IS NULL OR g.name = 'ALTRO')
  LIMIT 10""")])

# quante tavole hanno "POSTERIORE" nel nome ma nessun pezzo che lo dica?
for r in c.execute("""
  SELECT d.name, COUNT(*) AS n FROM part p
  JOIN drawing d ON d.revision_id = p.revision_id
  JOIN model_drawing md ON md.revision_id = p.revision_id
  WHERE md.model_id = 23672 AND d.name LIKE '%POSTERIOR%'
    AND p.description NOT LIKE '%posterior%'
  GROUP BY d.name ORDER BY n DESC LIMIT 10"""):
    print(dict(r))